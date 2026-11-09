"""Gemini and Gemma through the official google-genai SDK, using stateless generateContent (D8).

The SDK's own retries and automatic function calling are switched off: the RepoGym limiter
(D11) and retry policy own throttling, and the agent loop owns tool execution. Assistant content
is stored as the API returned it (``Message.raw``) and replayed verbatim, so thought signatures
are never altered (D12).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from repogym.providers.base import (
    Generation,
    Message,
    ToolCall,
    Usage,
    api_seconds,
    bad_response,
)
from repogym.providers.retry import Outcome, RetryPolicy, run_with_retries

PROVIDER = "gemini"
COUNT_TOKENS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
OK_FINISH = {"STOP"}


class NullLimiter:
    def acquire(self, estimated_tokens: int) -> float:
        return 0.0

    def settle(self, prompt_tokens: int | None) -> None:
        pass

    def mark_daily_exhausted(self) -> float | None:
        return None


@dataclass
class GeminiSettings:
    """Per-model settings from configs/models.yaml (D10); recorded in every episode."""

    model: str
    sampling: dict = field(default_factory=dict)  # temperature / top_p / top_k, or empty
    thinking_level: str | None = None  # None = the API default
    max_output_tokens: int = 8192

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "sampling": self.sampling,
            "thinking_level": self.thinking_level,
            "max_output_tokens": self.max_output_tokens,
        }


def tool_declarations(tools: list[dict]):
    from google.genai import types

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters_json_schema=t["parameters"],
                )
                for t in tools
            ]
        )
    ]


def to_contents(messages: list[Message]):
    """Neutral messages to SDK Contents. Assistant messages are replayed from ``raw``."""
    from google.genai import types

    contents = []
    for m in messages:
        if m.role == "assistant":
            if m.raw is None or m.provider != PROVIDER:
                raise ValueError("assistant message without Gemini-native content")
            contents.append(types.Content.model_validate(m.raw))
        elif m.role == "tool":
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=r.call_id, name=r.name, response={"output": r.output}
                            )
                        )
                        for r in m.results
                    ],
                )
            )
        else:
            parts = [types.Part(text=m.text)] + [types.Part(text=t) for t in m.extra_text]
            contents.append(types.Content(role="user", parts=parts))
    return contents


def from_content(content) -> Message:
    """The model's reply as a neutral message, keeping the native content verbatim."""
    raw = content.model_dump(mode="json", exclude_none=True)
    calls, texts = [], []
    for part in content.parts or []:
        if part.function_call is not None:
            calls.append(
                ToolCall(
                    id=part.function_call.id,
                    name=part.function_call.name or "",
                    args=dict(part.function_call.args or {}),
                )
            )
        elif part.text and not part.thought:
            texts.append(part.text)
    return Message(role="assistant", text="".join(texts), calls=calls, raw=raw, provider=PROVIDER)


def usage_of(meta) -> Usage:
    if meta is None:
        return Usage()
    return Usage(
        prompt_tokens=meta.prompt_token_count,
        cached_tokens=meta.cached_content_token_count,
        output_tokens=meta.candidates_token_count,
        thinking_tokens=meta.thoughts_token_count,
        total_tokens=meta.total_token_count,
    )


class GeminiProvider:
    def __init__(
        self,
        settings: GeminiSettings,
        api_key: str,
        limiter=None,
        policy: RetryPolicy | None = None,
        client=None,
        sleep=time.sleep,
        http=None,
        attempt_log=None,
    ) -> None:
        from google import genai
        from google.genai import types

        self.settings = settings
        self.name = settings.model
        self._no_retry = types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1), timeout=600_000
        )
        self.client = client or genai.Client(api_key=api_key, http_options=self._no_retry)
        self.limiter = limiter or NullLimiter()
        self.policy = policy or RetryPolicy()
        self._sleep = sleep
        self._api_key = api_key  # used only in request headers; never logged
        self._http = http  # httpx-like client for countTokens; created on first use
        self.attempt_log = attempt_log  # every attempt, for reliability analysis (D16)

    def _attempt_logger(self):
        """A per-call callback numbering this call's attempts into the attempt log."""
        if self.attempt_log is None:
            return None
        counter = {"n": 0}

        def record(attempt) -> None:
            counter["n"] += 1
            self.attempt_log.record(self.settings.model, attempt, counter["n"])

        return record

    def _config(self, system: str, tools: list[dict]):
        from google.genai import types

        thinking = (
            types.ThinkingConfig(thinking_level=self.settings.thinking_level)
            if self.settings.thinking_level
            else None
        )
        return types.GenerateContentConfig(
            system_instruction=system,
            tools=tool_declarations(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=thinking,
            max_output_tokens=self.settings.max_output_tokens,
            http_options=self._no_retry,
            **self.settings.sampling,
        )

    def generate(
        self, system: str, messages: list[Message], tools: list[dict], estimated_tokens: int = 0
    ) -> Generation:
        from google.genai import errors

        contents, config = to_contents(messages), self._config(system, tools)
        started, waits = time.monotonic(), []

        def send() -> Outcome:
            try:
                response = self.client.models.generate_content(
                    model=self.settings.model, contents=contents, config=config
                )
            except errors.APIError as exc:
                return Outcome(ok=False, status=exc.code, body=exc.details)
            except Exception as exc:  # transport errors (httpx etc.)
                return Outcome(ok=False, status=None, body={"error": type(exc).__name__})
            return Outcome(ok=True, status=200, value=response)

        response, attempts = run_with_retries(
            send,
            policy=self.policy,
            model=self.settings.model,
            sleep=self._sleep,
            before_attempt=lambda: waits.append(self.limiter.acquire(estimated_tokens) or 0.0),
            on_daily=self.limiter.mark_daily_exhausted,
            on_attempt=self._attempt_logger(),
        )
        usage = usage_of(response.usage_metadata)
        self.limiter.settle(usage.prompt_tokens)
        waited = sum(waits)
        latency = api_seconds(started, waited, attempts)
        candidate = (response.candidates or [None])[0]
        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else None
        if candidate is None or candidate.content is None or finish not in OK_FINISH:
            raise bad_response(f"{self.settings.model}: finish_reason {finish}", attempts, waited)
        message = from_content(candidate.content)
        if any(p.thought for p in candidate.content.parts or []):
            message.replay_tokens = 2 * (usage.thinking_tokens or 0)  # D21
        return Generation(
            message=message,
            usage=usage,
            finish_reason=finish,
            model_version=response.model_version,
            latency_seconds=latency,
            http_status=200,
            attempts=attempts,
            limiter_wait_seconds=waited,
        )

    def generate_text(
        self, system: str, prompt: str, max_tokens: int, estimated_tokens: int = 0
    ) -> Generation:
        """A plain, tool-free generation (used for context summaries), same limiter and policy."""
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            http_options=self._no_retry,
            thinking_config=(
                types.ThinkingConfig(thinking_level=self.settings.thinking_level)
                if self.settings.thinking_level
                else None
            ),
            **self.settings.sampling,
        )
        started, waits = time.monotonic(), []

        def send() -> Outcome:
            try:
                return Outcome(
                    True,
                    200,
                    self.client.models.generate_content(
                        model=self.settings.model, contents=prompt, config=config
                    ),
                )
            except errors.APIError as exc:
                return Outcome(False, exc.code, body=exc.details)
            except Exception as exc:  # noqa: BLE001 - transport errors
                return Outcome(False, None, body={"error": type(exc).__name__})

        response, attempts = run_with_retries(
            send,
            policy=self.policy,
            model=self.settings.model,
            sleep=self._sleep,
            before_attempt=lambda: waits.append(self.limiter.acquire(estimated_tokens) or 0.0),
            on_daily=self.limiter.mark_daily_exhausted,
            on_attempt=self._attempt_logger(),
        )
        usage = usage_of(response.usage_metadata)
        self.limiter.settle(usage.prompt_tokens)
        waited = sum(waits)
        candidate = (response.candidates or [None])[0]
        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else None
        # A summary cut off by max_tokens is still usable text; anything else is not.
        if (
            candidate is None
            or candidate.content is None
            or finish not in OK_FINISH | {"MAX_TOKENS"}
        ):
            raise bad_response(
                f"{self.settings.model}: summary finish_reason {finish}", attempts, waited
            )
        text = "".join(p.text or "" for p in candidate.content.parts or [] if not p.thought)
        return Generation(
            message=Message(role="assistant", text=text),
            usage=usage,
            finish_reason=finish,
            model_version=response.model_version,
            latency_seconds=api_seconds(started, waited, attempts),
            http_status=200,
            attempts=attempts,
            limiter_wait_seconds=waited,
        )

    def count_tokens(self, system: str, messages: list[Message], tools: list[dict]) -> int | None:
        """Exact prompt tokens via countTokens (not counted against RPM/TPM, D13).

        The whole request (system instruction, tools, contents) must be counted. The SDK's
        ``count_tokens`` refuses ``system_instruction`` in Developer API mode, so this posts a
        ``generateContentRequest`` to the REST method directly (verified within one token of
        the reported prompt_tokens; D13). Returns None on any error: the estimator is the
        fallback.
        """
        import httpx

        def dump(obj) -> dict:
            return obj.model_dump(mode="json", by_alias=True, exclude_none=True)

        if self._http is None:
            self._http = httpx.Client(timeout=60)
        try:
            body = {
                "generateContentRequest": {
                    "model": f"models/{self.settings.model}",
                    "contents": [dump(c) for c in to_contents(messages)],
                    "systemInstruction": {"parts": [{"text": system}]},
                    "tools": [dump(t) for t in tool_declarations(tools)],
                }
            }
            response = self._http.post(
                f"{COUNT_TOKENS_URL}/{self.settings.model}:countTokens",
                headers={"x-goog-api-key": self._api_key},
                json=body,
            )
            if response.status_code != 200:
                return None
            return int(response.json()["totalTokens"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return None
