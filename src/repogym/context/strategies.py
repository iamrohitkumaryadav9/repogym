"""The four context strategies compared in RQ1 (all under the same budget B and clip).

* sliding_window: drop the oldest complete units until the prompt fits (the budgeted baseline).
* obs_mask: keep the last M observations verbatim, replace older ones with a placeholder, then
  fall back to dropping the oldest units if it still doesn't fit.
* llm_summary: when the estimate exceeds ``summary_trigger`` x B, summarise all but the last K
  units (incrementally, with the same model), then drop the oldest units if still over B.
* hybrid: obs_mask first; only if the masked prompt still exceeds the trigger, summarise the
  oldest masked units; then drop if needed.

Every call to ``build`` is deterministic given the history and the stored summary state.
"""

from __future__ import annotations

from repogym.context.base import (
    MASK_TEXT,
    SUMMARY_HEADER,
    ContextConfig,
    ContextEvent,
    ContextOverflow,
    PromptView,
    Summarizer,
    Unit,
    clip_text,
    split_units,
    with_results,
)
from repogym.context.tokens import TokenEstimator
from repogym.providers.base import Message

STRATEGIES = ("sliding_window", "obs_mask", "llm_summary", "hybrid")


class ContextManager:
    def __init__(
        self,
        strategy: str,
        config: ContextConfig,
        estimator: TokenEstimator,
        summarizer: Summarizer | None = None,
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}; choose from {STRATEGIES}")
        self.strategy, self.config, self.estimator = strategy, config, estimator
        self.summarizer = summarizer
        self.summary: str | None = None
        self.summary_covers = 0  # units 1..n are represented by the summary
        self.summary_calls: list[dict] = []  # metrics of each summary call

    # -- building blocks ---------------------------------------------------------------------

    def _clipped(self, units: list[Unit]) -> tuple[dict[int, list[Message]], dict[str, int]]:
        """Every unit's messages with over-long observations clipped (all strategies)."""
        out, clipped = {}, {}
        for unit in units:
            messages = []
            for message in unit.messages:
                if message.role == "tool":
                    outputs = {}
                    for i, result in enumerate(message.results):
                        text, removed = clip_text(
                            result.output, self.config.clip_tokens, self.estimator
                        )
                        if removed:
                            outputs[i] = text
                            clipped[result.call_id or f"u{unit.index}r{i}"] = removed
                    messages.append(with_results(message, outputs) if outputs else message)
                else:
                    messages.append(message)
            out[unit.index] = messages
        return out, clipped

    def _masked(
        self, units: list[Unit], rendered: dict[int, list[Message]], states: dict[int, str]
    ) -> int:
        """Mask all but the last M observations (never in the latest unit). Returns how many."""
        keep, masked = self.config.keep_observations, 0
        seen = 0
        for unit in reversed(units):
            if states[unit.index] == "summarized":
                continue  # already represented by the summary; never shown again (determinism)
            for j in range(len(rendered[unit.index]) - 1, -1, -1):
                message = rendered[unit.index][j]
                if message.role != "tool":
                    continue
                outputs = {}
                for i in range(len(message.results) - 1, -1, -1):
                    seen += 1
                    if seen > keep and unit is not units[-1]:
                        result = unit.messages[j].results[i]
                        outputs[i] = MASK_TEXT.format(tool=result.name, chars=len(result.output))
                if outputs:
                    rendered[unit.index][j] = with_results(message, outputs)
                    states[unit.index] = "masked"
                    masked += len(outputs)
        return masked

    def _assemble(
        self,
        task: Message,
        units: list[Unit],
        rendered: dict[int, list[Message]],
        states: dict[int, str],
    ) -> list[Message]:
        head = task.copy()
        if self.summary and self.summary_covers:
            head.extra_text = [*task.extra_text, SUMMARY_HEADER + self.summary]
        messages = [head]
        for unit in units:
            if states[unit.index] not in ("dropped", "summarized"):
                messages += rendered[unit.index]
        return messages

    def _estimate(
        self, system: str, tools: list[dict], messages: list[Message]
    ) -> tuple[int, float]:
        return (
            self.estimator.estimate(system, tools, messages),
            self.estimator.raw(system, tools, messages),
        )

    def _summarize(self, units: list[Unit], upto: int, events: list[ContextEvent]) -> None:
        """Extend the summary to cover units 1..upto (incremental: only new units are sent)."""
        new = [u for u in units if self.summary_covers < u.index <= upto]
        if not new:
            return
        text, metrics = self.summarizer(self.summary, new)  # type: ignore[misc]
        self.summary, self.summary_covers = text, upto
        self.summary_calls.append({**metrics, "covers": upto})
        events.append(
            ContextEvent("summary", {"covers": upto, "units": [u.index for u in new], **metrics})
        )

    # -- the entry point ---------------------------------------------------------------------

    def build(self, system: str, tools: list[dict], history: list[Message]) -> PromptView:
        cfg = self.config
        if self.strategy in ("llm_summary", "hybrid") and self.summarizer is None:
            raise ValueError(f"{self.strategy} needs a summarizer")
        task, units = split_units(history)
        rendered, clipped = self._clipped(units)
        states = {u.index: "full" for u in units}
        for unit in units:
            if any(
                m.role == "tool" and any((r.call_id or "") in clipped for r in m.results)
                for m in unit.messages
            ):
                states[unit.index] = "clipped"
        for u in units:
            if u.index <= self.summary_covers:
                states[u.index] = "summarized"
        events: list[ContextEvent] = []
        if clipped:
            events.append(
                ContextEvent("clip", {"observations": len(clipped), "chars": sum(clipped.values())})
            )

        if self.strategy in ("obs_mask", "hybrid"):
            masked = self._masked(units, rendered, states)
            if masked:
                events.append(ContextEvent("mask", {"observations": masked}))

        messages = self._assemble(task, units, rendered, states)
        estimated, raw = self._estimate(system, tools, messages)

        if (
            self.strategy in ("llm_summary", "hybrid")
            and estimated > cfg.summary_trigger * cfg.budget
        ):
            upto = max(len(units) - cfg.keep_recent_units, 0)
            if upto > self.summary_covers:
                self._summarize(units, upto, events)
                for u in units:
                    if u.index <= self.summary_covers:
                        states[u.index] = "summarized"
                messages = self._assemble(task, units, rendered, states)
                estimated, raw = self._estimate(system, tools, messages)

        # Drop the oldest whole units until it fits (sliding_window's rule; the others' fallback).
        dropped = 0
        while estimated > cfg.budget:
            candidates = [u for u in units[:-1] if states[u.index] not in ("dropped", "summarized")]
            if not candidates:
                break
            states[candidates[0].index] = "dropped"
            dropped += 1
            messages = self._assemble(task, units, rendered, states)
            estimated, raw = self._estimate(system, tools, messages)
        if dropped:
            kind = "drop" if self.strategy == "sliding_window" else "fallback"
            events.append(ContextEvent(kind, {"units_dropped": dropped}))

        fits = estimated <= cfg.budget
        view = PromptView(
            messages=messages,
            estimated_tokens=estimated,
            raw_estimate=raw,
            replay_tokens=self.estimator.replay(messages),
            units=states,
            clipped=clipped,
            events=events,
            summary=self.summary,
            summary_covers=self.summary_covers,
            over_budget=not fits,
        )
        if view.over_budget:
            events.append(ContextEvent("overflow", {"estimated": estimated}))
            raise ContextOverflow(
                f"pinned prefix + latest unit is ~{estimated} tokens > B={cfg.budget}"
            )
        return view
