"""Context management: building each prompt so it fits a token budget B (RQ1; D12, D14, D15).

History model:
* ``history[0]`` is the task statement (a user message). With the system prompt and the tool
  schemas, it forms the **pinned prefix**, which is always sent verbatim.
* After it, the history is a sequence of **units**. Each unit is one assistant message plus the
  messages answering it: its tool results, or a nudge when the reply had no tool call.

Every strategy keeps these invariants (checked by ``check_view``, and by property tests):
1. the pinned prefix is verbatim (a summary may only be *appended* to the task message);
2. the latest unit is always kept in full (clipped like everything else, never masked or
   dropped);
3. a call is never separated from its response, and assistant messages are never edited, so
   thought signatures survive (D12);
4. the output is deterministic for the same history and summary state.

Common to every strategy, so not a confound: any single observation over ``clip_tokens`` is
clipped head+tail with an explicit marker. All of this is prompt-only; the trajectory keeps the
full observation (D14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from repogym.context.tokens import TokenEstimator
from repogym.providers.base import Message, ToolResult

CLIP_MARKER = "[... {chars} characters (~{tokens} tokens) clipped by RepoGym ...]"
MASK_TEXT = "[observation from `{tool}` omitted to save context: {chars} characters]"
SUMMARY_HEADER = (
    "Summary of earlier steps (written by you, to save context; the steps "
    "themselves are no longer shown):\n"
)


@dataclass
class Unit:
    index: int  # 1-based position after the task message
    messages: list[Message]

    @property
    def assistant(self) -> Message:
        return self.messages[0]


@dataclass
class ContextEvent:
    kind: str  # clip | mask | summary | drop | fallback | overflow
    detail: dict = field(default_factory=dict)


@dataclass
class PromptView:
    """The prompt to send, plus a compact record of how it was made from the full history."""

    messages: list[Message]
    estimated_tokens: int
    raw_estimate: float
    units: dict[int, str]  # unit index -> full | clipped | masked | summarized | dropped
    clipped: dict[str, int]  # tool call id -> characters clipped
    events: list[ContextEvent] = field(default_factory=list)
    summary: str | None = None
    summary_covers: int = 0  # units 1..summary_covers are represented by the summary
    over_budget: bool = False  # True only if even the minimal view doesn't fit
    exact_tokens: int | None = None  # countTokens result, when exact counting is on

    def spec(self) -> dict:
        """What the model saw, small enough to store for every call (D14)."""
        return {
            "units": self.units,
            "clipped": self.clipped,
            "summary_covers": self.summary_covers,
            "estimated_tokens": self.estimated_tokens,
            "exact_tokens": self.exact_tokens,
            "events": [e.kind for e in self.events],
        }


class ContextOverflow(RuntimeError):
    """Even the pinned prefix plus the latest unit exceeds B: the call can't be made."""


def split_units(history: list[Message]) -> tuple[Message, list[Unit]]:
    if not history or history[0].role != "user":
        raise ValueError("history must start with the task message")
    units: list[Unit] = []
    for message in history[1:]:
        if message.role == "assistant" or not units:
            units.append(Unit(index=len(units) + 1, messages=[message]))
        else:
            units[-1].messages.append(message)
    return history[0], units


def clip_text(text: str, clip_tokens: int, estimator: TokenEstimator) -> tuple[str, int]:
    """Head+tail clip to about ``clip_tokens``; returns the new text and characters removed."""
    if estimator.text_tokens(text) <= clip_tokens:
        return text, 0
    keep = max(estimator.chars_for(clip_tokens) - 120, 200)
    head, tail = keep // 2, keep - keep // 2
    removed = len(text) - head - tail
    marker = CLIP_MARKER.format(chars=removed, tokens=estimator.text_tokens(text[head:-tail]))
    return f"{text[:head]}\n{marker}\n{text[-tail:]}", removed


def with_results(message: Message, outputs: dict[int, str]) -> Message:
    """A copy of a tool message with some results' outputs replaced (ids and names kept)."""
    copy = message.copy()
    copy.results = [
        ToolResult(r.call_id, r.name, outputs.get(i, r.output))
        for i, r in enumerate(message.results)
    ]
    return copy


class Summarizer(Protocol):
    def __call__(self, previous_summary: str | None, units: list[Unit]) -> tuple[str, dict]:
        """A new summary covering the previous summary plus ``units``, and call metrics."""
        ...


@dataclass
class ContextConfig:
    budget: int = 12_000  # B, in prompt tokens
    clip_tokens: int = 2_000
    keep_observations: int = 5  # M (obs_mask, hybrid)
    keep_recent_units: int = 4  # K (llm_summary, hybrid)
    summary_trigger: float = 0.8  # summarise when the estimate exceeds this fraction of B
    summary_max_tokens: int = 1_000

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def check_view(history: list[Message], view: PromptView) -> list[str]:
    """Invariant violations of ``view`` against ``history`` (empty when it's valid)."""
    problems = []
    task, units = split_units(history)
    sent = view.messages
    if not sent or sent[0].role != "user" or sent[0].text != task.text:
        problems.append("pinned task statement is not verbatim")
    # Assistant messages are sent unedited, each immediately followed by its results.
    originals = [m for m in history if m.role == "assistant"]
    for i, message in enumerate(sent):
        if message.role == "assistant":
            if not any(message.raw == o.raw and message.calls == o.calls for o in originals):
                problems.append(f"assistant message {i} was edited")
            if message.calls:
                nxt = sent[i + 1] if i + 1 < len(sent) else None
                ids = [(c.id, c.name) for c in message.calls]
                got = (
                    [(r.call_id, r.name) for r in nxt.results] if nxt and nxt.role == "tool" else []
                )
                if ids != got and units[-1].assistant is not message:
                    problems.append(f"calls at {i} are not followed by their results")
        if message.role == "tool":
            prev = sent[i - 1] if i else None
            if (
                prev is None
                or prev.role != "assistant"
                or [(c.id, c.name) for c in prev.calls]
                != [(r.call_id, r.name) for r in message.results]
            ):
                problems.append(f"tool results at {i} have no matching calls (orphaned)")
    if units:
        last = units[-1]
        tail = sent[-len(last.messages) :]
        if len(sent) < len(last.messages) + 1 or tail[0].raw != last.assistant.raw:
            problems.append("latest unit is not kept")
        elif any(MASK_TEXT.split("{")[0] in r.output for m in tail for r in m.results):
            problems.append("latest unit was masked")
    return problems
