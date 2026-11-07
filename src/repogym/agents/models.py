"""Provider-agnostic chat models for agents (design D4).

An agent sees only ``ChatModel.chat(system, turns) -> Reply``. Providers live in one small class
each and are chosen by name from config. A provider must:

* keep the API key out of logs, reprs, errors and trajectories;
* retry 429/500/503 with jittered exponential backoff, then fall back (Gemini already does,
  through the audited client in ``repogym.llm``);
* treat any finish reason other than a normal stop as a failure (``ModelResponseError``); a cut
  off or blocked reply is never parsed as an action;
* report token usage, and cost only from a configured price, never a guess.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from dotenv import load_dotenv

from repogym.config import ROOT
from repogym.llm import (
    GeminiClient,
    LLMBadResponse,
    LLMRequestError,
    LLMUnavailable,
    Settings,
    backoff_delays,
    jittered,
)

# USD per million tokens (input, output). Only models listed here get a cost; others record None.
# gemini-3.5-flash-lite is used on the free tier, so its marginal cost is recorded as 0.
PRICES: dict[str, tuple[float, float]] = {"gemini-3.5-flash-lite": (0.0, 0.0)}


class ModelUnavailable(RuntimeError):
    """Retries and fallback exhausted (quota, outage)."""


class ModelResponseError(RuntimeError):
    """The model answered, but not with a complete reply (blocked, cut off, empty)."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None = None
    finish_reason: str | None = None

    @property
    def cost_usd(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None or self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return (self.prompt_tokens * price[0] + self.completion_tokens * price[1]) / 1e6

    def metrics(self) -> dict:
        out = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_usd": self.cost_usd,
        }
        return {k: v for k, v in out.items() if v is not None}


class ChatModel(Protocol):
    name: str

    def chat(self, system: str, turns: list[tuple[str, str]]) -> Reply:
        """``turns`` alternate ("user", text) and ("assistant", text), starting with user."""
        ...


@dataclass
class GeminiChat:
    """Gemini through the audited client (same retries, fallback and finishReason rules)."""

    client: GeminiClient
    temperature: float = 0.0
    max_output_tokens: int = 8192
    seed: int | None = None

    @property
    def name(self) -> str:
        return self.client.settings.model

    def chat(self, system: str, turns: list[tuple[str, str]]) -> Reply:
        config: dict = {"temperature": self.temperature, "maxOutputTokens": self.max_output_tokens}
        if self.seed is not None:
            config["seed"] = self.seed
        roles = [("model" if role == "assistant" else "user", text) for role, text in turns]
        try:
            done = self.client.chat(system, roles, config)
        except LLMUnavailable as exc:
            raise ModelUnavailable(str(exc)) from exc
        except LLMBadResponse as exc:
            raise ModelResponseError(str(exc)) from exc
        except LLMRequestError as exc:
            raise ModelUnavailable(str(exc)) from exc
        return Reply(
            done.text,
            done.model,
            done.prompt_tokens,
            done.completion_tokens,
            done.cached_tokens,
            done.finish_reason,
        )


@dataclass
class OpenAICompatChat:
    """Any OpenAI-compatible chat-completions endpoint, configured from .env:
    OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY, OPENAI_COMPAT_MODEL."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    temperature: float = 0.0
    max_tokens: int = 8192
    interval: float = 1.0
    transport: object | None = None
    sleep: object = time.sleep

    @classmethod
    def from_env(cls) -> OpenAICompatChat:
        load_dotenv(ROOT / ".env")
        names = ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL")
        missing = [n for n in names if not os.environ.get(n, "").strip()]
        if missing:
            raise ModelUnavailable(f"Missing in .env: {', '.join(missing)}")
        return cls(
            os.environ[names[0]].strip().rstrip("/"),
            os.environ[names[2]].strip(),
            os.environ[names[1]].strip(),
        )

    @property
    def name(self) -> str:
        return self.model

    def chat(self, system: str, turns: list[tuple[str, str]]) -> Reply:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}]
            + [{"role": role, "content": text} for role, text in turns],
        }
        post = self.transport or (
            lambda body: httpx.post(
                f"{self.base_url}/chat/completions",
                json=body,
                timeout=180,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        )
        delays, rng = backoff_delays(), random.Random()
        for attempt in range(len(delays) + 1):
            try:
                response = post(payload)
            except httpx.TransportError:
                status = None
            else:
                status = response.status_code
                if status == 200:
                    return self._parse(response.json())
                if status not in (429, 500, 503):
                    raise ModelUnavailable(f"{self.model} returned HTTP {status}")
            if attempt < len(delays):
                self.sleep(jittered(delays[attempt], rng))
        raise ModelUnavailable(f"{self.model}: {len(delays) + 1} attempts failed")

    def _parse(self, data: dict) -> Reply:
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError(f"unexpected response shape: {type(exc).__name__}") from exc
        if finish != "stop":
            raise ModelResponseError(f"finish_reason {finish}")
        if not text.strip():
            raise ModelResponseError("empty reply")
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        return Reply(
            text,
            self.model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            cached,
            finish,
        )


def from_config(provider: str = "gemini", interval: float = 6.0) -> ChatModel:
    """The configured chat model. Keys come from .env and are never printed."""
    if provider == "gemini":
        return GeminiChat(GeminiClient(Settings.from_env(), interval=interval))
    if provider == "openai-compat":
        return OpenAICompatChat.from_env()
    raise ValueError(f"unknown provider {provider!r}")
