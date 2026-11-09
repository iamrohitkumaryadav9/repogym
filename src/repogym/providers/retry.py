"""The retry policy shared by every provider (D11, D16, and Week 2's audited client behaviour).

* 429 with a quota id naming a per-day quota, or a retry delay over ``max_honoured_delay``:
  daily exhaustion. The same applies when the limiter finds today's quota used up. With
  ``pause_on_daily`` the call is held in place until the reset (plus a margin) and then retried,
  so the episode keeps its sandbox and state. Otherwise it stops at once and the worker sleeps
  until the reset.
* 429 otherwise: per-minute throttling. Wait at least the server's ``retryDelay``, or a jittered
  exponential backoff, for up to ``max_attempts`` tries.
* 5xx and transport errors: transient server trouble. Retry in place with jittered exponential
  backoff (``server_base_delay`` doubling to ``server_max_delay``). Give up only after
  ``server_max_failures`` consecutive failures or ``server_max_seconds`` since the first,
  whichever comes first.
* Any other 4xx: rejected at once.

Every attempt is recorded with its time, and can be streamed to an attempt log.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from repogym.providers.base import (
    Attempt,
    DailyQuotaExhausted,
    ProviderUnavailable,
    RequestRejected,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6  # per-minute 429s
    base_delay: float = 2.0
    max_delay: float = 120.0
    max_honoured_delay: float = 300.0  # a longer server delay means a daily quota
    # 5xx and transport errors: pause in place (D16, amended 2026-11-09).
    server_base_delay: float = 30.0
    server_max_delay: float = 600.0
    server_max_failures: int = 20  # consecutive failed attempts of one call
    server_max_seconds: float = 3600.0  # wall time since that call's first failure
    # Daily quota: hold the call in place until the reset (experiments turn this on).
    pause_on_daily: bool = False
    max_daily_pauses: int = 1
    reset_margin: float = 60.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def retry_info(body) -> tuple[float | None, list[str]]:
    """RetryInfo.retryDelay (seconds) and QuotaFailure quota ids from a Google error body."""
    error = body.get("error", body) if isinstance(body, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    delay, quota_ids = None, []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        raw = detail.get("retryDelay")
        if isinstance(raw, str) and raw.endswith("s"):
            try:
                delay = float(raw[:-1])
            except ValueError:
                pass
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict) and violation.get("quotaId"):
                quota_ids.append(str(violation["quotaId"]))
    return delay, quota_ids


def is_daily(quota_ids: list[str], delay: float | None, policy: RetryPolicy) -> bool:
    return any("PerDay" in q for q in quota_ids) or (
        delay is not None and delay > policy.max_honoured_delay
    )


@dataclass
class Outcome:
    """What one attempt produced: a result, or an error status and its body."""

    ok: bool
    status: int | None
    value: object = None
    body: object = None


def run_with_retries(
    send: Callable[[], Outcome],
    *,
    policy: RetryPolicy,
    model: str,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    before_attempt: Callable[[], None] | None = None,
    on_daily: Callable[[], float | None] | None = None,
    on_attempt: Callable[[Attempt], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
):
    """Call ``send`` until it succeeds. Returns (value, attempts)."""
    rng = rng or random.Random()
    attempts: list[Attempt] = []
    rate_limited = server_failures = daily_pauses = 0
    first_failure: float | None = None

    def add(attempt: Attempt) -> None:
        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(attempt)

    def pause_for_reset(reset_at: float | None, status, quota_ids) -> bool:
        """Hold in place until the reset; False if pausing isn't allowed (any more)."""
        nonlocal daily_pauses
        if not policy.pause_on_daily or daily_pauses >= policy.max_daily_pauses or not reset_at:
            return False
        daily_pauses += 1
        wait = max(reset_at - wall(), 0.0) + policy.reset_margin
        add(Attempt(status, "daily_quota_pause", None, wait, list(quota_ids), at=wall()))
        sleep(wait)
        return True

    while True:
        if before_attempt is not None:
            try:
                before_attempt()
            except DailyQuotaExhausted as exc:  # the limiter: today's quota is used up
                if pause_for_reset(exc.reset_at, None, []):
                    continue
                exc.attempts = attempts + list(exc.attempts)
                raise
        at = wall()
        outcome = send()
        if outcome.ok:
            add(Attempt(status=outcome.status, kind="ok", at=at))
            return outcome.value, attempts
        status = outcome.status
        delay, quota_ids = retry_info(outcome.body) if status == 429 else (None, [])
        if status == 429 and is_daily(quota_ids, delay, policy):
            reset_at = on_daily() if on_daily else None
            if pause_for_reset(reset_at, status, quota_ids):
                continue
            add(Attempt(status, "daily_quota", delay, 0.0, quota_ids, at=at))
            raise DailyQuotaExhausted(
                f"{model}: daily quota exhausted ({', '.join(quota_ids)})",
                reset_at=reset_at,
                attempts=attempts,
            )
        if status is not None and 400 <= status < 500 and status != 429:
            add(Attempt(status, "rejected", at=at))
            raise RequestRejected(f"{model} returned HTTP {status}", attempts=attempts)
        if status == 429:
            rate_limited += 1
            backoff = min(policy.base_delay * 2 ** (rate_limited - 1), policy.max_delay)
            wait = max(backoff * rng.uniform(0.5, 1.5), delay or 0.0)
            if rate_limited >= policy.max_attempts:
                add(Attempt(status, "rate_limited", delay, 0.0, quota_ids, at=at))
                raise ProviderUnavailable(
                    f"{model}: rate limited {rate_limited} times in a row", attempts
                )
            add(Attempt(status, "rate_limited", delay, wait, quota_ids, at=at))
            sleep(wait)
            continue
        # 5xx or a transport error: transient server trouble, retried in place.
        kind = "transport_error" if status is None else "server_error"
        server_failures += 1
        first_failure = clock() if first_failure is None else first_failure
        elapsed = clock() - first_failure
        if server_failures >= policy.server_max_failures or elapsed >= policy.server_max_seconds:
            add(Attempt(status, kind, None, 0.0, at=at))
            raise ProviderUnavailable(
                f"{model}: {server_failures} consecutive server errors over {elapsed / 60:.0f} min",
                attempts,
            )
        backoff = policy.server_base_delay * 2 ** (server_failures - 1)
        wait = min(backoff * rng.uniform(0.5, 1.5), policy.server_max_delay)
        wait = min(wait, max(policy.server_max_seconds - elapsed, 0.0))
        add(Attempt(status, kind, None, wait, at=at))
        sleep(wait)
