"""An OpenAI-compatible chat-completions provider with native tool calling (Ollama, vLLM, …).

Configured from configs/models.yaml (base URL and model); the key comes from .env. Same
interface, retry policy and limiter as the Gemini provider (D9). Assistant messages keep the
returned message dict as ``raw`` and are replayed verbatim.
"""

from __future__ import annotations

import json
import time

import httpx

from repogym.providers.base import (
    BadResponse,
    Generation,
    Message,
    ProviderError,
    ToolCall,
    Usage,
    api_seconds,
    bad_response,
)
from repogym.providers.gemini import NullLimiter
from repogym.providers.retry import Outcome, RetryPolicy, run_with_retries

PROVIDER = "openai-compat"


def to_messages(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "assistant":
            if m.raw is None or m.provider != PROVIDER:
                raise ValueError("assistant message without OpenAI-compatible native content")
            out.append(m.raw)
        elif m.role == "tool":
            out += [
                {"role": "tool", "tool_call_id": r.call_id, "content": r.output} for r in m.results
            ]
        else:
            out.append({"role": "user", "content": "\n\n".join([m.text, *m.extra_text])})
    return out


def from_message(raw: dict) -> Message:
    calls = []
    for call in raw.get("tool_calls") or []:
        fn = call.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise BadResponse(f"tool call arguments are not JSON: {exc.msg}") from exc
        calls.append(ToolCall(id=call.get("id"), name=fn.get("name", ""), args=args))
    return Message(
        role="assistant",
        text=raw.get("content") or "",
        calls=calls,
        raw={k: v for k, v in raw.items() if k in ("role", "content", "tool_calls")},
        provider=PROVIDER,
    )


class OpenAICompatProvider:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "",
        limiter=None,
        policy: RetryPolicy | None = None,
        transport=None,
        sleep=time.sleep,
        sampling: dict | None = None,
        max_output_tokens: int = 8192,
        attempt_log=None,
    ) -> None:
        self.name = model
        self.attempt_log = attempt_log
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.limiter = limiter or NullLimiter()
        self.policy = policy or RetryPolicy()
        self._transport = transport
        self._sleep = sleep
        self.sampling = sampling or {}
        self.max_output_tokens = max_output_tokens

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatProvider(model={self.name!r}, base_url={self.base_url!r})"

    def _post(self, body: dict) -> httpx.Response:
        if self._transport is not None:
            return self._transport(body)
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        return httpx.post(
            f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=600
        )

    def _call(self, body: dict, estimated_tokens: int):
        started, waits = time.monotonic(), []

        def send() -> Outcome:
            try:
                r = self._post(body)
            except httpx.TransportError as exc:
                return Outcome(False, None, body={"error": type(exc).__name__})
            if r.status_code != 200:
                try:
                    detail = r.json()
                except ValueError:
                    detail = {}
                return Outcome(False, r.status_code, body=detail)
            return Outcome(True, 200, r.json())

        try:
            data, attempts = run_with_retries(
                send,
                policy=self.policy,
                model=self.name,
                sleep=self._sleep,
                before_attempt=lambda: waits.append(self.limiter.acquire(estimated_tokens) or 0.0),
                on_daily=self.limiter.mark_daily_exhausted,
                on_attempt=self._attempt_logger(),
            )
        except ProviderError as exc:
            exc.wait_seconds += sum(waits)  # the limiter's share (D17)
            raise
        u = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=u.get("prompt_tokens"),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
            output_tokens=u.get("completion_tokens"),
            thinking_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            total_tokens=u.get("total_tokens"),
        )
        self.limiter.settle(usage.prompt_tokens)
        waited = sum(waits)
        return data, usage, attempts, api_seconds(started, waited, attempts), waited

    def _attempt_logger(self):
        if self.attempt_log is None:
            return None
        counter = {"n": 0}

        def record(attempt) -> None:
            counter["n"] += 1
            self.attempt_log.record(self.name, attempt, counter["n"])

        return record

    def generate(
        self, system: str, messages: list[Message], tools: list[dict], estimated_tokens: int = 0
    ) -> Generation:
        body = {
            "model": self.name,
            "messages": to_messages(system, messages),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ],
            "max_tokens": self.max_output_tokens,
            **self.sampling,
        }
        data, usage, attempts, latency, waited = self._call(body, estimated_tokens)
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise bad_response(f"{self.name}: no choices", attempts, waited) from exc
        finish = choice.get("finish_reason")
        if finish not in ("stop", "tool_calls"):
            raise bad_response(f"{self.name}: finish_reason {finish}", attempts, waited)
        return Generation(
            message=from_message(choice.get("message") or {}),
            usage=usage,
            finish_reason=finish,
            model_version=data.get("model"),
            latency_seconds=latency,
            http_status=200,
            attempts=attempts,
            limiter_wait_seconds=waited,
        )

    def generate_text(
        self, system: str, prompt: str, max_tokens: int, estimated_tokens: int = 0
    ) -> Generation:
        body = {
            "model": self.name,
            "max_tokens": max_tokens,
            **self.sampling,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        data, usage, attempts, latency, waited = self._call(body, estimated_tokens)
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") not in ("stop", "length"):
            raise bad_response(
                f"{self.name}: summary finish_reason {choice.get('finish_reason')}",
                attempts,
                waited,
            )
        text = (choice.get("message") or {}).get("content") or ""
        return Generation(
            message=Message(role="assistant", text=text),
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            model_version=data.get("model"),
            latency_seconds=latency,
            http_status=200,
            attempts=attempts,
            limiter_wait_seconds=waited,
        )
