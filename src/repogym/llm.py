"""Gemini client: backoff with jitter, model fallback, throttling and a response cache.

The API key is read from .env and only ever sent in a request header, never in a URL, log line
or cache file.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from dotenv import load_dotenv

from repogym.config import CACHE_DIR, ROOT

log = structlog.get_logger()

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY_STATUSES = {429, 500, 503}
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 2.0
DEFAULT_INTERVAL_SECONDS = 5.0
LLM_CACHE_DIR = CACHE_DIR / "llm"
REQUIRED_ENV = ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL")


class MissingSettings(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    """Every attempt on every model failed with a retryable error."""


class LLMRequestError(RuntimeError):
    """A non-retryable error, such as a bad request or a rejected key."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    model: str
    fallback_model: str

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
        if missing:
            raise MissingSettings(f"Missing in .env: {', '.join(missing)}")
        return cls(
            api_key=os.environ["GEMINI_API_KEY"].strip(),
            model=os.environ["GEMINI_MODEL"].strip(),
            fallback_model=os.environ["GEMINI_FALLBACK_MODEL"].strip(),
        )


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    cached: bool


def backoff_delays(attempts: int = MAX_ATTEMPTS, base: float = BASE_DELAY_SECONDS) -> list[float]:
    """Sleeps between attempts: 2, 4, 8, 16 for five attempts (before jitter)."""
    return [base * 2**i for i in range(attempts - 1)]


def jittered(delay: float, rng: random.Random) -> float:
    """Full-range jitter around the nominal delay, so parallel clients don't retry in lockstep."""
    return delay * rng.uniform(0.5, 1.5)


class Throttle:
    """At most one request per ``interval`` seconds, across threads."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


Transport = Callable[[str, str, dict], httpx.Response]


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        cache_dir: Path = LLM_CACHE_DIR,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self._cache_dir = cache_dir
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._throttle = Throttle(interval, sleep=sleep)
        self._http = httpx.Client(timeout=120) if transport is None else None
        self._transport = transport or self._post
        self.requests = 0

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    # -- cache -------------------------------------------------------------------------------

    def _cache_path(self, model: str, key: str) -> Path:
        digest = hashlib.sha256(f"{model}\n{key}".encode()).hexdigest()
        return self._cache_dir / digest[:2] / f"{digest}.json"

    def cached(self, model: str, key: str) -> str | None:
        path = self._cache_path(model, key)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        return None

    def store(self, model: str, key: str, text: str) -> None:
        path = self._cache_path(model, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"model": model, "text": text}), encoding="utf-8")
        temp.replace(path)

    def forget(self, model: str, key: str) -> None:
        self._cache_path(model, key).unlink(missing_ok=True)

    # -- requests ----------------------------------------------------------------------------

    def _post(self, model: str, prompt: str, generation_config: dict) -> httpx.Response:
        assert self._http is not None
        return self._http.post(
            f"{API_ROOT}/{model}:generateContent",
            headers={"x-goog-api-key": self.settings.api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            },
        )

    def _call_with_retries(self, model: str, prompt: str, generation_config: dict) -> str:
        delays = backoff_delays()
        for attempt in range(MAX_ATTEMPTS):
            self._throttle.wait()
            self.requests += 1
            try:
                response = self._transport(model, prompt, generation_config)
            except httpx.TransportError as exc:
                status, reason = None, type(exc).__name__
            else:
                status, reason = response.status_code, ""
                if status == 200:
                    return _extract_text(response)
                if status not in RETRY_STATUSES:
                    # Report the status only: error bodies can echo request details.
                    raise LLMRequestError(f"{model} returned HTTP {status}")
            if attempt < MAX_ATTEMPTS - 1:
                wait = jittered(delays[attempt], self._rng)
                log.warning("llm_retry", model=model, status=status or reason, sleep=round(wait, 1))
                self._sleep(wait)
        raise LLMUnavailable(f"{model}: {MAX_ATTEMPTS} attempts failed")

    def complete(self, prompt: str, cache_key: str, generation_config: dict,
                 use_cache: bool = True) -> Completion:  # fmt: skip
        """Primary model first (cache, then API); the fallback only once the primary is exhausted.

        A rerun always tries the primary again before reusing a fallback answer, so items that
        needed the fallback move back to the primary model as soon as it is available.
        """
        primary, fallback = self.settings.model, self.settings.fallback_model
        if use_cache and (text := self.cached(primary, cache_key)) is not None:
            return Completion(text, primary, cached=True)
        try:
            text = self._call_with_retries(primary, prompt, generation_config)
            self.store(primary, cache_key, text)
            return Completion(text, primary, cached=False)
        except LLMUnavailable:
            log.warning("llm_fallback", primary=primary, fallback=fallback)
        if use_cache and (text := self.cached(fallback, cache_key)) is not None:
            return Completion(text, fallback, cached=True)
        text = self._call_with_retries(fallback, prompt, generation_config)
        self.store(fallback, cache_key, text)
        return Completion(text, fallback, cached=False)


def _extract_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError(f"Unexpected response shape: {type(exc).__name__}") from exc
