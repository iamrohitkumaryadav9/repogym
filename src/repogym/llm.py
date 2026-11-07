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


class LLMBadResponse(LLMRequestError):
    """HTTP 200 but no usable answer: no candidates, empty text, or a finishReason other than
    STOP (SAFETY, MAX_TOKENS, RECITATION, ...), which means the text may be partial."""


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
    finish_reason: str | None = None  # None only for cache entries written before it was kept


@dataclass(frozen=True)
class ChatCompletion:
    text: str
    model: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None = None


@dataclass(frozen=True)
class CacheProblem:
    path: Path
    model: str | None
    problem: str


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
        self._chat_transport: Callable[[str, dict], httpx.Response] | None = None
        self.requests = 0

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    # -- cache -------------------------------------------------------------------------------

    def _cache_path(self, model: str, key: str) -> Path:
        digest = hashlib.sha256(f"{model}\n{key}".encode()).hexdigest()
        return self._cache_dir / digest[:2] / f"{digest}.json"

    def cached(self, model: str, key: str) -> Completion | None:
        path = self._cache_path(model, key)
        if path.exists():
            entry = json.loads(path.read_text(encoding="utf-8"))
            return Completion(entry["text"], model, cached=True,
                              finish_reason=entry.get("finish_reason"))  # fmt: skip
        return None

    def store(self, model: str, key: str, text: str, finish_reason: str) -> None:
        path = self._cache_path(model, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        entry = {"model": model, "finish_reason": finish_reason, "text": text}
        temp.write_text(json.dumps(entry), encoding="utf-8")
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

    def _retrying(self, model: str, send: Callable[[], httpx.Response]) -> httpx.Response:
        """One request with the shared policy: throttle, retry 429/500/503 and network errors
        with jittered exponential backoff (5 attempts), fail fast on anything else."""
        delays = backoff_delays()
        for attempt in range(MAX_ATTEMPTS):
            self._throttle.wait()
            self.requests += 1
            try:
                response = send()
            except httpx.TransportError as exc:
                status, reason = None, type(exc).__name__
            else:
                status, reason = response.status_code, ""
                if status == 200:
                    return response
                if status not in RETRY_STATUSES:
                    # Report the status only: error bodies can echo request details.
                    raise LLMRequestError(f"{model} returned HTTP {status}")
            if attempt < MAX_ATTEMPTS - 1:
                wait = jittered(delays[attempt], self._rng)
                log.warning("llm_retry", model=model, status=status or reason, sleep=round(wait, 1))
                self._sleep(wait)
        raise LLMUnavailable(f"{model}: {MAX_ATTEMPTS} attempts failed")

    def _call_with_retries(
        self, model: str, prompt: str, generation_config: dict
    ) -> tuple[str, str]:
        response = self._retrying(model, lambda: self._transport(model, prompt, generation_config))
        return _extract_text(response)

    # -- multi-turn chat (agents) --------------------------------------------------------------

    def _post_chat(self, model: str, payload: dict) -> httpx.Response:
        assert self._http is not None
        return self._http.post(
            f"{API_ROOT}/{model}:generateContent",
            headers={"x-goog-api-key": self.settings.api_key},
            json=payload,
        )

    def chat(
        self,
        system: str,
        turns: list[tuple[str, str]],
        generation_config: dict,
        chat_transport: Callable[[str, dict], httpx.Response] | None = None,
    ) -> ChatCompletion:
        """A multi-turn request (``turns`` are ("user" | "model", text) pairs), with the same
        retry, fallback and finishReason rules as ``complete``. Never cached: an agent's turns
        are a trajectory, not a lookup."""
        send = chat_transport or self._chat_transport or self._post_chat
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": role, "parts": [{"text": text}]} for role, text in turns],
            "generationConfig": generation_config,
        }
        primary, fallback = self.settings.model, self.settings.fallback_model
        models = [primary] if fallback == primary else [primary, fallback]
        for index, model in enumerate(models):
            try:
                response = self._retrying(model, lambda m=model: send(m, payload))
            except LLMUnavailable:
                if index == len(models) - 1:
                    raise
                log.warning("llm_fallback", primary=primary, fallback=fallback)
                continue
            text, finish_reason = _extract_text(response)
            usage = response.json().get("usageMetadata") or {}
            return ChatCompletion(
                text=text,
                model=model,
                finish_reason=finish_reason,
                prompt_tokens=usage.get("promptTokenCount"),
                completion_tokens=(usage.get("candidatesTokenCount") or 0)
                + (usage.get("thoughtsTokenCount") or 0),
                cached_tokens=usage.get("cachedContentTokenCount"),
            )
        raise LLMUnavailable("no model available")

    def _fresh(self, model: str, prompt: str, cache_key: str, generation_config: dict,
               validate: Callable[[str], object] | None) -> Completion:  # fmt: skip
        text, finish_reason = self._call_with_retries(model, prompt, generation_config)
        if validate is not None:
            validate(text)  # raises before anything is cached
        self.store(model, cache_key, text, finish_reason)
        return Completion(text, model, cached=False, finish_reason=finish_reason)

    def complete(
        self,
        prompt: str,
        cache_key: str,
        generation_config: dict,
        use_cache: bool = True,
        validate: Callable[[str], object] | None = None,
    ) -> Completion:
        """Primary model first (cache, then API); the fallback only once the primary is exhausted.

        Only answers that pass ``validate`` (and finished with STOP) are cached. A rerun always
        tries the primary again before reusing a fallback answer. When the fallback is the same
        model as the primary there is no second round.
        """
        primary, fallback = self.settings.model, self.settings.fallback_model
        if use_cache and (hit := self.cached(primary, cache_key)) is not None:
            return hit
        try:
            return self._fresh(primary, prompt, cache_key, generation_config, validate)
        except LLMUnavailable:
            if fallback == primary:
                raise
            log.warning("llm_fallback", primary=primary, fallback=fallback)
        if use_cache and (hit := self.cached(fallback, cache_key)) is not None:
            return hit
        return self._fresh(fallback, prompt, cache_key, generation_config, validate)

    def audit_cache(self, validate: Callable[[str], object]) -> tuple[int, list[CacheProblem]]:
        """Check every cache entry: readable, STOP (or unrecorded), and passes ``validate``."""
        paths = sorted(self._cache_dir.rglob("*.json"))
        problems = []
        for path in paths:
            model = None
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                model = entry.get("model")
                if entry.get("finish_reason") not in (None, "STOP"):
                    raise ValueError(f"finishReason {entry['finish_reason']}")
                if not str(entry.get("text", "")).strip():
                    raise ValueError("empty text")
                validate(entry["text"])
            except Exception as exc:  # noqa: BLE001 - any failure makes the entry unusable
                problems.append(CacheProblem(path, model, f"{type(exc).__name__}: {exc}"))
        problems += [CacheProblem(p, None, "leftover temp file")
                     for p in self._cache_dir.rglob("*.tmp")]  # fmt: skip
        return len(paths), problems


def _extract_text(response: httpx.Response) -> tuple[str, str]:
    """The answer text and its finishReason; anything short of a complete answer raises."""
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMBadResponse("response is not JSON") from exc
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason") if data else None
        raise LLMBadResponse(f"no candidates (blockReason {blocked})" if blocked
                             else "no candidates")  # fmt: skip
    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason != "STOP":
        raise LLMBadResponse(f"finishReason {finish_reason}")
    try:
        parts = candidate["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
    except (KeyError, TypeError) as exc:
        raise LLMBadResponse(f"unexpected response shape: {type(exc).__name__}") from exc
    if not text.strip():
        raise LLMBadResponse("empty text")
    return text, finish_reason
