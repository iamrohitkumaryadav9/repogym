"""The Week 3 agent: native function calling over all six tools, under a context strategy.

It acts ONLY through the environment API, exactly like the scripted agents:
* ``env.step(Action)`` for every tool call, run in the sandbox as the agent user;
* ``env.note`` for a reply with no usable tool call;
* ``env.record_event`` for context events.

It never touches the sandbox, grading or the anti-cheating checks.

Each LLM call:
1. the ContextManager builds a view of the full history that fits B;
2. the provider makes the call, with the limiter and retries;
3. each returned call becomes one env step.

The full observation goes into the history, and the trajectory stores both it and a record of
what the model saw (D14).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from repogym.context.base import ContextOverflow, Unit, clip_text
from repogym.context.strategies import ContextManager
from repogym.env.environment import Action, Environment, Observation
from repogym.providers.base import BadResponse, Message, ToolResult
from repogym.tools.definitions import TOOLS

SYSTEM_PROMPT = """You are a software engineer resolving a GitHub issue in a Python repository.
The repository is at /work, checked out at the commit before the fix. Explore the code, reproduce
the problem if you can, edit the source to fix it, run the relevant tests, and call submit.
Hidden tests are run after you submit; changes to test files are discarded before grading.
There is no network access.

How to work:
- In every reply, before your tool call, write one or two sentences: what you learned from the
  last result and what you will do next. Your own messages are kept much longer than tool
  results (see below), so write down what you'll need later: file paths, line numbers,
  function names, the cause of the bug.
- Find code with `grep -n`, then view narrow ranges (view_file shows at most 150 lines).
- Once you understand the cause, edit the source (str_replace), run the relevant test
  (run_tests), and submit. Don't keep exploring once the fix works.

What you can see:
{view}
- After each tool result you'll see [step k/{max_steps}]: you have {max_steps} tool calls in
  total."""

VIEW_TEXT = {
    "clip": (
        "- A tool result longer than about {clip_tokens} tokens is shown with its middle cut out:"
        " the start and end are kept, with a marker saying how much was removed."
    ),
    "obs_mask": (
        "- Only the results of your last {keep_observations} tool calls are shown; older results"
        " are replaced by a one-line placeholder. Your own messages and tool calls are kept."
    ),
    "sliding_window": (
        "- When the conversation gets too long, the oldest steps (your messages, tool calls and"
        " their results) are removed."
    ),
    "llm_summary": (
        "- When the conversation gets too long, older steps are replaced by a summary written for"
        " you; the most recent steps stay in full."
    ),
    "fallback": ("- If the conversation still doesn't fit, the oldest steps are removed."),
}
STRATEGY_VIEW = {
    "sliding_window": ("clip", "sliding_window"),
    "obs_mask": ("clip", "obs_mask", "fallback"),
    "llm_summary": ("clip", "llm_summary", "fallback"),
    "hybrid": ("clip", "obs_mask", "llm_summary", "fallback"),
}
STEP_COUNTER = "[step {k}/{n}]"
FINAL_STEPS = 15  # one nudge when this many tool calls remain
FINAL_NUDGE = (
    "[You have used {k} of {n} tool calls. If you haven't fixed the issue yet, make the edit now;"
    " then run the relevant test and submit.]"
)
REASONING_REMINDER = (
    "[Reminder: before each tool call, write one or two sentences on what you learned and what"
    " you'll do next.]"
)
REMIND_EVERY = 2  # consecutive replies without reasoning before a reminder


def system_prompt_for(strategy: str, config, max_steps: int) -> str:
    """The system prompt, describing exactly what this strategy shows the model."""
    view = "\n".join(
        VIEW_TEXT[part].format(
            clip_tokens=config.clip_tokens, keep_observations=config.keep_observations
        )
        for part in STRATEGY_VIEW[strategy]
    )
    return SYSTEM_PROMPT.format(view=view, max_steps=max_steps)


NO_TOOL_NUDGE = "You must call one of the tools. Call submit when you are done."
SUMMARY_SYSTEM = (
    "You summarise an AI coding agent's progress so far. Keep: what was learned "
    "about the code (files, functions, causes), what was changed, test results, "
    "and what remains to do. Be concise and factual; don't invent anything."
)


def render_unit(unit: Unit, clip_tokens: int, estimator) -> str:
    lines = []
    for message in unit.messages:
        if message.role == "assistant":
            if message.text:
                lines.append(f"Agent: {message.text}")
            for call in message.calls:
                lines.append(f"Tool call: {call.name} {json.dumps(call.args, ensure_ascii=False)}")
        elif message.role == "tool":
            for result in message.results:
                text, _ = clip_text(result.output, clip_tokens, estimator)
                lines.append(f"Result of {result.name}:\n{text}")
        else:
            lines.append(f"Harness: {message.text}")
    return "\n".join(lines)


def waiting(gen, wall: float | None) -> dict:
    """Throttling inside the provider call (exact, from the provider) and the call's wall time.

    The waiting part is reported to the environment and excluded from the active-time cap (D17).
    """
    out = {
        "limiter_wait_seconds": round(gen.limiter_wait_seconds, 3),
        "backoff_seconds": round(gen.backoff_seconds, 3),
        "waiting_seconds": round(gen.wait_seconds, 3),
    }
    if wall is not None:
        out["call_wall_seconds"] = round(wall, 3)
    return out


def call_metrics(gen, view, budget: int, record: dict, wall: float | None = None) -> dict:
    """ATIF step metrics for one LLM call, with RepoGym's per-call record in ``extra``."""
    u = gen.usage
    output, thinking = u.output_tokens or 0, u.thinking_tokens or 0
    return {
        "prompt_tokens": u.prompt_tokens or 0,
        "completion_tokens": output + thinking,  # ATIF: includes reasoning
        "cached_tokens": u.cached_tokens or 0,
        "extra": {
            "repogym": {
                "output_tokens": output,
                "thinking_tokens": thinking,
                "total_tokens": u.total_tokens,
                "latency_seconds": gen.latency_seconds,
                **waiting(gen, wall),
                "finish_reason": gen.finish_reason,
                "http_status": gen.http_status,
                "attempts": len(gen.attempts),
                "retries": gen.retries,
                "rate_limited": sum(a.kind == "rate_limited" for a in gen.attempts),
                "attempt_log": [
                    {
                        "status": a.status,
                        "kind": a.kind,
                        "slept": round(a.slept, 2),
                        "retry_delay": a.retry_delay,
                    }
                    for a in gen.attempts
                ],
                "model_version": gen.model_version,
                "estimated_tokens": view.estimated_tokens,
                "exact_tokens": view.exact_tokens,
                "budget": budget,
                "budget_violation": bool(u.prompt_tokens and u.prompt_tokens > budget),
                "estimation_error": record.get("error"),
                "view": view.spec(),
            }
        },
    }


@dataclass
class ToolAgent:
    provider: object  # a ToolProvider
    context: ContextManager
    name: str = "repogym-tool-agent"
    version: str = "1.0"
    system_prompt: str | None = None  # built per strategy and step budget when None
    settings: dict = field(default_factory=dict)  # recorded in provenance
    # Called with the env step count after every model call (the runner's heartbeat).
    progress: Callable[[int], None] | None = None
    # The environment's first message defers to this agent's description of its view (D14).
    instruction_style: str = "tool_agent"

    @property
    def model_name(self) -> str:
        return self.provider.name

    @property
    def quota_ids_seen(self) -> set[str]:
        return set()

    def summarizer(self, env: Environment):
        """A Summarizer for llm_summary/hybrid that calls the same model through its limiter."""
        cfg, est = self.context.config, self.context.estimator

        def summarize(previous: str | None, units: list[Unit]) -> tuple[str, dict]:
            body = "\n\n".join(render_unit(u, cfg.clip_tokens, est) for u in units)
            prompt = (
                f"Summary so far:\n{previous}\n\nNew steps:\n{body}"
                if previous
                else f"Steps:\n{body}"
            ) + "\n\nWrite the updated summary."
            t0 = time.monotonic()
            gen = self.provider.generate_text(
                SUMMARY_SYSTEM,
                prompt,
                cfg.summary_max_tokens,
                estimated_tokens=est.text_tokens(prompt),
            )
            wall = time.monotonic() - t0
            env.record_wait(gen.wait_seconds)
            u = gen.usage
            metrics = {
                "prompt_tokens": u.prompt_tokens,
                "cached_tokens": u.cached_tokens,
                "output_tokens": u.output_tokens,
                "thinking_tokens": u.thinking_tokens,
                "attempts": len(gen.attempts),
                "retries": gen.retries,
                "rate_limited": sum(a.kind == "rate_limited" for a in gen.attempts),
                "latency_seconds": gen.latency_seconds,
                **waiting(gen, wall),
                "finish_reason": gen.finish_reason,
                "model_version": gen.model_version,
            }
            env.record_event(
                "summary",
                {"units": [x.index for x in units]},
                message=gen.message.text,
                metrics=metrics,
            )
            return gen.message.text, metrics

        return summarize

    def run(self, env: Environment, first: Observation) -> None:
        if self.context.strategy in ("llm_summary", "hybrid"):
            self.context.summarizer = self.summarizer(env)
        budget = self.context.config.budget
        max_steps = env.state().budgets.max_steps
        if self.system_prompt is None:
            self.system_prompt = system_prompt_for(
                self.context.strategy, self.context.config, max_steps
            )
        history: list[Message] = [Message(role="user", text=first.text)]
        no_reason_streak, nudged = 0, False

        def annotate(text: str, remind: bool) -> str:
            """What the model sees after a tool result: the step counter, and nudges."""
            nonlocal nudged
            k = env.state().step_count
            notes = [STEP_COUNTER.format(k=k, n=max_steps)]
            if not nudged and k >= max_steps - FINAL_STEPS:
                nudged = True
                notes.append(FINAL_NUDGE.format(k=k, n=max_steps))
                env.record_event("final_steps_nudge", {"step": k})
            if remind:
                notes.append(REASONING_REMINDER)
                env.record_event("reasoning_reminder", {"step": k})
            return f"{text}\n" + "\n".join(notes)

        while not env.state().done:
            try:
                view = self.context.build(self.system_prompt, TOOLS, history)
            except ContextOverflow as exc:
                env.record_event("overflow", {"error": str(exc)})
                raise
            for event in view.events:
                if event.kind != "summary":  # summary events are recorded with their metrics
                    env.record_event(event.kind, event.detail)
            t0 = time.monotonic()
            try:
                gen = self.provider.generate(
                    self.system_prompt,
                    view.messages,
                    TOOLS,
                    estimated_tokens=view.exact_tokens or view.estimated_tokens,
                )
            except BadResponse as exc:
                env.record_wait(exc.wait_seconds)
                # Blocked, truncated or a malformed call: never an action. It counts as a step.
                done, _ = env.note(
                    f"Your last reply could not be used ({exc}). {NO_TOOL_NUDGE}",
                    model_name=self.provider.name,
                    llm_calls=1,
                )
                if self.progress:
                    self.progress(env.state().step_count)
                if done:
                    return
                continue
            wall = time.monotonic() - t0
            env.record_wait(gen.wait_seconds)
            record = self.context.estimator.observe(
                view.raw_estimate,
                view.estimated_tokens,
                gen.usage.prompt_tokens,
                view.replay_tokens,
            )
            metrics = call_metrics(gen, view, budget, record, wall)
            reply = gen.message
            history.append(reply)
            if not reply.calls:
                done, _ = env.note(
                    NO_TOOL_NUDGE,
                    message=reply.text,
                    model_name=self.provider.name,
                    metrics=metrics,
                    llm_calls=1,
                )
                history.append(Message(role="user", text=annotate(NO_TOOL_NUDGE, False)))
                if self.progress:
                    self.progress(env.state().step_count)
                if done:
                    return
                continue
            remind = False
            if reply.text.strip():
                no_reason_streak = 0
            else:
                # Not rejected: the call still runs. Recorded, and after REMIND_EVERY in a row
                # the next result carries one short reminder.
                no_reason_streak += 1
                env.record_event("missing_reasoning", {"streak": no_reason_streak})
                remind = no_reason_streak % REMIND_EVERY == 0
            results: list[ToolResult] = []
            for k, call in enumerate(reply.calls):
                # The environment assigns a unique step id for the trajectory; the provider's
                # call id (which may be reused or empty) is kept only to match the response.
                observation, done, _ = env.step(
                    Action(
                        tool=call.name,
                        arguments=call.args,
                        message=reply.text if k == 0 else "",
                        model_name=self.provider.name,
                        metrics=metrics if k == 0 else None,
                        llm_calls=1 if k == 0 else 0,
                    )
                )
                results.append(ToolResult(call.id, call.name, observation.text))
                if done:
                    break
            # The counter (and any nudge) follows the last result of this reply.
            last = results[-1]
            results[-1] = ToolResult(last.call_id, last.name, annotate(last.output, remind))
            if self.progress:
                self.progress(env.state().step_count)
            if env.state().done:
                return
            history.append(Message(role="tool", results=results))
