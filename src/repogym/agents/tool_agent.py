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
from dataclasses import dataclass, field

from repogym.context.base import ContextOverflow, Unit, clip_text
from repogym.context.strategies import ContextManager
from repogym.env.environment import Action, Environment, Observation
from repogym.providers.base import BadResponse, Message, ToolResult
from repogym.tools.definitions import TOOLS

SYSTEM_PROMPT = """You are a software engineer resolving a GitHub issue in a Python repository.
The repository is at /work, checked out at the commit before the fix. Use the tools to explore
the code, reproduce the problem if you can, edit the source to fix it, and check your change.
Hidden tests are run after you submit; changes to test files are discarded before grading.
There is no network access. Call exactly the tools you need; when you are done, call submit.
Older observations may be shortened or summarised to fit the context budget."""

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


def call_metrics(gen, view, budget: int, record: dict) -> dict:
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
    system_prompt: str = SYSTEM_PROMPT
    settings: dict = field(default_factory=dict)  # recorded in provenance
    # Log countTokens next to the estimate (D13); it never changes what is sent.
    shadow_count: bool = False

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
            gen = self.provider.generate_text(
                SUMMARY_SYSTEM,
                prompt,
                cfg.summary_max_tokens,
                estimated_tokens=est.text_tokens(prompt),
            )
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
        history: list[Message] = [Message(role="user", text=first.text)]
        while not env.state().done:
            try:
                view = self.context.build(self.system_prompt, TOOLS, history)
            except ContextOverflow as exc:
                env.record_event("overflow", {"error": str(exc)})
                raise
            for event in view.events:
                if event.kind != "summary":  # summary events are recorded with their metrics
                    env.record_event(event.kind, event.detail)
            try:
                gen = self.provider.generate(
                    self.system_prompt,
                    view.messages,
                    TOOLS,
                    estimated_tokens=view.exact_tokens or view.estimated_tokens,
                )
            except BadResponse as exc:
                # Blocked, truncated or a malformed call: never an action. It counts as a step.
                done, _ = env.note(
                    f"Your last reply could not be used ({exc}). {NO_TOOL_NUDGE}",
                    model_name=self.provider.name,
                    llm_calls=1,
                )
                if done:
                    return
                continue
            shadow = (
                self.provider.count_tokens(self.system_prompt, view.messages, TOOLS)
                if self.shadow_count
                else None
            )
            record = self.context.estimator.observe(
                view.raw_estimate, view.estimated_tokens, gen.usage.prompt_tokens
            )
            metrics = call_metrics(gen, view, budget, record)
            metrics["extra"]["repogym"]["count_tokens_shadow"] = shadow
            reply = gen.message
            history.append(reply)
            if not reply.calls:
                history.append(Message(role="user", text=NO_TOOL_NUDGE))
                done, _ = env.note(
                    NO_TOOL_NUDGE,
                    message=reply.text,
                    model_name=self.provider.name,
                    metrics=metrics,
                    llm_calls=1,
                )
                if done:
                    return
                continue
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
                    return
            history.append(Message(role="tool", results=results))
