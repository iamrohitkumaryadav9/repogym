"""Per-model rate limiting and a persistent daily-request ledger (D11).

* RPM and TPM (input tokens) are enforced over a rolling 60-second window, at a safety margin
  (90% by default).
* TPM accounting reserves the pre-call estimate, then replaces it with the API's reported
  prompt tokens.
* RPD is counted in a ledger on disk, keyed by the Pacific calendar date. Quotas reset at
  midnight Pacific time, and DST is handled by zoneinfo. The ledger survives restarts.
* When a model's daily quota is used up, by the ledger or by a daily-quota 429, ``acquire``
  raises DailyQuotaExhausted with the next reset time, so that model's worker can sleep while
  the others keep running.

Every HTTP attempt counts as a request, retries included: it isn't documented whether throttled
requests count toward the daily quota, so we assume they do.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from repogym.providers.base import DailyQuotaExhausted

WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ModelLimits:
    rpm: int
    tpm: int
    rpd: int


@dataclass(frozen=True)
class LimitsConfig:
    models: dict[str, ModelLimits]
    margin: float
    reset_timezone: str
    source: str
    verified: str

    @classmethod
    def load(cls, path: Path) -> LimitsConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            models={k: ModelLimits(**v) for k, v in data["models"].items()},
            margin=float(data.get("margin", 0.9)),
            reset_timezone=data.get("reset_timezone", "America/Los_Angeles"),
            source=data.get("source", ""),
            verified=str(data.get("verified", "")),
        )

    def effective(self, model: str) -> dict[str, int]:
        """The limits actually enforced (dashboard limit x margin, rounded down)."""
        limits = self.models[model]
        return {
            "rpm": max(1, math.floor(limits.rpm * self.margin)),
            "tpm": max(1, math.floor(limits.tpm * self.margin)),
            "rpd": max(1, math.floor(limits.rpd * self.margin)),
        }


class QuotaLedger:
    """Requests per model per quota day, persisted as JSON (atomic writes, thread-safe)."""

    def __init__(self, path: Path, timezone: str = "America/Los_Angeles") -> None:
        self.path = path
        self.tz = ZoneInfo(timezone)
        self._lock = threading.Lock()
        self._data: dict = json.loads(path.read_text()) if path.exists() else {}

    def day(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).date().isoformat()

    def next_reset(self, ts: float) -> float:
        """Epoch seconds of the next midnight in the reset time zone (DST-correct)."""
        local = datetime.fromtimestamp(ts, self.tz)
        midnight = datetime(local.year, local.month, local.day, tzinfo=self.tz) + timedelta(days=1)
        return midnight.timestamp()

    def _entry(self, model: str, ts: float) -> dict:
        return self._data.setdefault(self.day(ts), {}).setdefault(
            model, {"requests": 0, "exhausted": False}
        )

    def requests(self, model: str, ts: float) -> int:
        with self._lock:
            return self._data.get(self.day(ts), {}).get(model, {}).get("requests", 0)

    def exhausted(self, model: str, ts: float) -> bool:
        with self._lock:
            return self._data.get(self.day(ts), {}).get(model, {}).get("exhausted", False)

    def add(self, model: str, ts: float, n: int = 1) -> int:
        with self._lock:
            entry = self._entry(model, ts)
            entry["requests"] += n
            self._save()
            return entry["requests"]

    def mark_exhausted(self, model: str, ts: float) -> None:
        with self._lock:
            self._entry(model, ts)["exhausted"] = True
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


class RateLimiter:
    """One model's limiter. ``acquire`` blocks until a request fits the minute limits, and
    raises DailyQuotaExhausted when the daily quota is used up."""

    def __init__(
        self,
        model: str,
        config: LimitsConfig,
        ledger: QuotaLedger,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.limits = config.effective(model)
        self.config = config
        self.ledger = ledger
        self._clock, self._sleep = clock, sleep
        self._window: deque[list[float]] = deque()  # [timestamp, input tokens]
        self._lock = threading.Lock()
        self.waited_seconds = 0.0

    def _prune(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= WINDOW_SECONDS:
            self._window.popleft()

    def _daily_check(self, now: float) -> None:
        if self.ledger.exhausted(self.model, now) or (
            self.ledger.requests(self.model, now) >= self.limits["rpd"]
        ):
            raise DailyQuotaExhausted(
                f"{self.model}: daily quota reached "
                f"({self.ledger.requests(self.model, now)} requests)",
                reset_at=self.ledger.next_reset(now),
            )

    def acquire(self, estimated_tokens: int) -> float:
        """Block until the request fits; returns the seconds this call waited."""
        estimated_tokens = max(int(estimated_tokens or 0), 0)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._daily_check(now)
                self._prune(now)
                used = sum(tokens for _, tokens in self._window)
                fits_rpm = len(self._window) < self.limits["rpm"]
                # An empty window always admits one request, even one larger than the TPM
                # limit; otherwise such a request could never be sent.
                fits_tpm = not self._window or used + estimated_tokens <= self.limits["tpm"]
                if fits_rpm and fits_tpm:
                    self._window.append([now, float(estimated_tokens)])
                    self.ledger.add(self.model, now)
                    return waited
                wait = max(WINDOW_SECONDS - (now - self._window[0][0]), 0.05)
            self.waited_seconds += wait
            waited += wait
            self._sleep(wait)

    def settle(self, prompt_tokens: int | None) -> None:
        """Replace the latest reservation with the API's reported prompt tokens."""
        if prompt_tokens is None:
            return
        with self._lock:
            if self._window:
                self._window[-1][1] = float(prompt_tokens)

    def mark_daily_exhausted(self) -> float:
        now = self._clock()
        self.ledger.mark_exhausted(self.model, now)
        return self.ledger.next_reset(now)

    def window_usage(self) -> dict[str, float]:
        with self._lock:
            self._prune(self._clock())
            return {
                "requests": len(self._window),
                "tokens": sum(tokens for _, tokens in self._window),
            }


class AttemptLog:
    """Every API attempt (time, model, status, kind, attempt number within its call), one JSON
    line each, in a file per UTC day. The basis for error rates by hour (D16)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def record(self, model: str, attempt, number: int) -> None:
        at = attempt.at or time.time()
        day = datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d")
        row = {
            "at": round(at, 3),
            "model": model,
            "attempt": number,
            "status": attempt.status,
            "kind": attempt.kind,
            "slept": round(attempt.slept or 0.0, 3),
        }
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / f"attempts-{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
