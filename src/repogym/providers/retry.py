"""The retry policy shared by every provider (D11, and Week 2's audited client behaviour).

* 429 with a quota id naming a per-day quota, or a retry delay over ``max_honoured_delay``:
  daily exhaustion. Stop at once, and mark the limiter so the worker sleeps until the reset.
* 429 otherwise: per-minute throttling. Wait at least the server's ``retryDelay``, or a jittered
  exponential backoff.
* 5xx and transport errors: jittered exponential backoff.
* Any other 4xx: rejected at once.

Retries are capped, and every attempt is recorded.
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
    max_attempts: int = 6
    base_delay: float = 2.0
    max_delay: float = 120.0
    max_honoured_delay: float = 300.0  # a longer server delay means a daily quota


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
):
    """Call ``send`` until it succeeds. Returns (value, attempts)."""
    rng = rng or random.Random()
    attempts: list[Attempt] = []
    for n in range(policy.max_attempts):
        if before_attempt is not None:
            before_attempt()
        outcome = send()
        if outcome.ok:
            attempts.append(Attempt(status=outcome.status, kind="ok"))
            return outcome.value, attempts
        status = outcome.status
        delay, quota_ids = retry_info(outcome.body) if status == 429 else (None, [])
        if status == 429 and is_daily(quota_ids, delay, policy):
            attempts.append(Attempt(status, "daily_quota", delay, 0.0, quota_ids))
            reset_at = on_daily() if on_daily else None
            raise DailyQuotaExhausted(
                f"{model}: daily quota exhausted ({', '.join(quota_ids)})",
                reset_at=reset_at,
                attempts=attempts,
            )
        if status is not None and 400 <= status < 500 and status != 429:
            attempts.append(Attempt(status, "rejected"))
            raise RequestRejected(f"{model} returned HTTP {status}", attempts=attempts)
        kind = (
            "rate_limited"
            if status == 429
            else "transport_error"
            if status is None
            else "server_error"
        )
        backoff = min(policy.base_delay * 2**n, policy.max_delay) * rng.uniform(0.5, 1.5)
        wait = max(backoff, delay or 0.0)
        last = n == policy.max_attempts - 1
        attempts.append(Attempt(status, kind, delay, 0.0 if last else wait, quota_ids))
        if not last:
            sleep(wait)
    raise ProviderUnavailable(f"{model}: {policy.max_attempts} attempts failed", attempts)
