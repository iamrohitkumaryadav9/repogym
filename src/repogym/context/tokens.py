"""Prompt token estimation, calibrated online within each episode (D13, D15).

The estimate starts conservative: few characters per token, so it over-counts. After each call
it learns the ratio of the API's reported prompt tokens to its raw estimate (an exponential
moving average, bounded), and every call's estimation error is logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from repogym.providers.base import Message

DEFAULT_CHARS_PER_TOKEN = 2.5  # conservative: code and JSON usually run 3-4 chars per token
PER_MESSAGE_OVERHEAD = 8  # role markers and part framing
SAFETY = 1.05  # always aim slightly above the calibrated estimate


def message_chars(message: Message) -> int:
    n = len(message.text) + sum(len(t) for t in message.extra_text)
    for call in message.calls:
        n += len(call.name) + len(json.dumps(call.args, ensure_ascii=False))
    for result in message.results:
        n += len(result.name) + len(result.output) + 20
    return n


def prompt_chars(system: str, tools: list[dict], messages: list[Message]) -> int:
    return (
        len(system)
        + len(json.dumps(tools, ensure_ascii=False))
        + sum(message_chars(m) for m in messages)
    )


@dataclass
class TokenEstimator:
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
    factor: float = 1.0  # learned ratio reported / raw estimate
    alpha: float = 0.3
    bounds: tuple[float, float] = (0.3, 3.0)
    history: list[dict] = field(default_factory=list)  # per-call estimation records

    def raw(self, system: str, tools: list[dict], messages: list[Message]) -> float:
        return prompt_chars(
            system, tools, messages
        ) / self.chars_per_token + PER_MESSAGE_OVERHEAD * (len(messages) + 1)

    def estimate(self, system: str, tools: list[dict], messages: list[Message]) -> int:
        return int(self.raw(system, tools, messages) * self.factor * SAFETY) + 1

    def text_tokens(self, text: str) -> int:
        return int(len(text) / self.chars_per_token * self.factor * SAFETY) + 1

    def chars_for(self, tokens: int) -> int:
        """How many characters fit in ``tokens`` under the current (conservative) estimate."""
        return max(int(tokens * self.chars_per_token / (self.factor * SAFETY)), 0)

    def observe(self, raw_estimate: float, estimated: int, reported: int | None) -> dict:
        """Record one call's error and update the calibration factor."""
        record = {
            "estimated": estimated,
            "reported": reported,
            "error": None if not reported else round((estimated - reported) / reported, 4),
            "factor_before": round(self.factor, 4),
        }
        if reported and raw_estimate > 0:
            ratio = min(max(reported / raw_estimate, self.bounds[0]), self.bounds[1])
            self.factor = (1 - self.alpha) * self.factor + self.alpha * ratio
        record["factor_after"] = round(self.factor, 4)
        self.history.append(record)
        return record
