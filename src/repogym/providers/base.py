"""The tool-calling provider interface (D9): provider-neutral messages, usage and errors.

An assistant message keeps the provider's native content in ``raw`` and is replayed verbatim on
later calls, so thought signatures (D12) are never lost or re-serialised. Context strategies
edit only user and tool messages.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    call_id: str | None
    name: str
    output: str


@dataclass
class Message:
    role: Role
    text: str = ""  # user text, or the assistant's visible (non-thought) text
    calls: list[ToolCall] = field(default_factory=list)  # assistant
    results: list[ToolResult] = field(default_factory=list)  # tool
    # Extra user text parts after ``text`` (e.g. a summary): ``text`` stays verbatim.
    extra_text: list[str] = field(default_factory=list)
    raw: dict | None = None  # provider-native assistant content, replayed byte-for-byte
    provider: str | None = None  # which provider produced ``raw``
    # Input tokens the API bills each time this message is replayed beyond its visible text
    # and calls: Gemma returns its thinking as a thought-text part, which is billed twice
    # (2 x its thinking tokens, measured; D21). Exact, so not scaled by the estimator.
    replay_tokens: int = 0

    def copy(self) -> Message:
        return copy.deepcopy(self)


@dataclass
class Usage:
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None  # visible output (excludes thinking)
    thinking_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class Attempt:
    """One HTTP attempt of a call (successful or retried)."""

    status: int | None  # HTTP status, or None for a transport error
    # ok | rate_limited | daily_quota | daily_quota_pause | server_error | transport_error
    # | rejected
    kind: str
    retry_delay: float | None = None  # what the server asked for, if anything
    slept: float = 0.0
    quota_ids: list[str] = field(default_factory=list)
    at: float | None = None  # wall-clock time the attempt was made (epoch seconds)


@dataclass
class Generation:
    message: Message
    usage: Usage
    finish_reason: str | None
    model_version: str | None
    latency_seconds: float
    http_status: int
    attempts: list[Attempt] = field(default_factory=list)
    limiter_wait_seconds: float = 0.0  # time the rate limiter held the request back

    @property
    def retries(self) -> int:
        return max(len(self.attempts) - 1, 0)

    @property
    def backoff_seconds(self) -> float:
        return sum(a.slept or 0.0 for a in self.attempts)

    @property
    def quota_pause_seconds(self) -> float:
        """Time held in place across a daily-quota reset (D16); also excluded from the
        elapsed safety cap."""
        return sum(a.slept or 0.0 for a in self.attempts if a.kind == "daily_quota_pause")

    @property
    def wait_seconds(self) -> float:
        """Throttling time inside the call (limiter + retry backoff); excluded from the
        active-time cap (D17)."""
        return self.limiter_wait_seconds + self.backoff_seconds


class ProviderError(RuntimeError):
    def __init__(self, message: str, attempts: list[Attempt] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []
        self.wait_seconds = 0.0  # set by providers when known (limiter + backoff)


class DailyQuotaExhausted(ProviderError):
    """The model's daily request quota is used up; retry after the reset (D11)."""

    def __init__(
        self, message: str, reset_at: float | None = None, attempts: list[Attempt] | None = None
    ) -> None:
        super().__init__(message, attempts)
        self.reset_at = reset_at


class ProviderUnavailable(ProviderError):
    """Retries exhausted on per-minute throttling, server errors or transport errors."""


class RequestRejected(ProviderError):
    """A non-retryable 4xx (bad request, invalid key, disabled account)."""


class BadResponse(ProviderError):
    """HTTP 200, but no usable reply: blocked, truncated or a malformed function call. The agent
    treats this as a step it must retry, never as an action."""


class ToolProvider(Protocol):
    name: str  # the configured model id

    def generate(
        self, system: str, messages: list[Message], tools: list[dict], estimated_tokens: int = 0
    ) -> Generation: ...

    def generate_text(
        self, system: str, prompt: str, max_tokens: int, estimated_tokens: int = 0
    ) -> Generation: ...


def api_seconds(started: float, waited: float, attempts: list) -> float:
    """Wall time of the call minus limiter waits and retry backoff: time spent on the API."""
    backoff = sum(a.slept or 0.0 for a in attempts)
    return round(max(time.monotonic() - started - waited - backoff, 0.0), 3)


def bad_response(message: str, attempts: list, waited: float) -> BadResponse:
    exc = BadResponse(message, attempts)
    exc.wait_seconds = waited + sum(a.slept or 0.0 for a in attempts)
    return exc
