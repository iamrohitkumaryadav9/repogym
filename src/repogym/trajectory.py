"""ATIF trajectories (Agent Trajectory Interchange Format, Harbor RFC-0001) and their validator.

Target version: ATIF-v1.8 (the version the RFC marks current, 2026-11). The validator below
implements the RFC's required fields, types, allowed values and cross-rules, so it runs in CI
without Harbor (which needs Python >= 3.12 and ~100 dependencies). It's cross-checked locally
against Harbor's own validator; see docs/design/week2_environment.md, D5.
"""

from __future__ import annotations

from typing import Any

from repogym.env.environment import StepRecord
from repogym.tools.definitions import openai_style

SCHEMA_VERSION = "ATIF-v1.8"
# RepoGym context events -> ATIF context_management (type, boundary). A summary is appended to
# the pinned task message; masking, clipping and dropping prune older content.
CONTEXT_TYPES = {
    "summary": ("compaction", "append"),
    "mask": ("pruning", "truncate"),
    "clip": ("pruning", "truncate"),
    "drop": ("pruning", "truncate"),
    "fallback": ("pruning", "truncate"),
    "overflow": ("overflow", "none"),
}
SOURCES = {"system", "user", "agent"}
AGENT_ONLY = {"model_name", "reasoning_effort", "reasoning_content", "tool_calls", "metrics"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
AUDIO_TYPES = {
    "audio/wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/webm",
    "audio/aiff",
}


def build(
    *,
    episode_id: str,
    session_id: str,
    agent_name: str,
    agent_version: str,
    model_name: str | None,
    system_text: str,
    user_text: str,
    started_at: str,
    steps: list[StepRecord],
    final_extra: dict,
    events: list | None = None,
    root_extra: dict,
    token_totals: dict | None = None,
) -> dict:
    """An ATIF document for one episode. Scripted steps are deterministic dispatches
    (llm_call_count 0, no metrics); LLM steps carry the metrics the agent recorded."""
    agent: dict[str, Any] = {
        "name": agent_name,
        "version": agent_version,
        "tool_definitions": openai_style(),
    }
    if model_name:
        agent["model_name"] = model_name
    out_steps: list[dict[str, Any]] = [
        {"step_id": 1, "timestamp": started_at, "source": "system", "message": system_text},
        {"step_id": 2, "timestamp": started_at, "source": "user", "message": user_text},
    ]
    pending = sorted(events or [], key=lambda e: e.after_step)

    def flush(upto: int) -> None:
        """Context events as ATIF system steps (RFC convention: extra.context_management)."""
        while pending and pending[0].after_step <= upto:
            event = pending.pop(0)
            extra: dict[str, Any] = {
                "repogym": {"event": event.kind, "detail": event.detail, "metrics": event.metrics}
            }
            if event.kind in CONTEXT_TYPES:  # other events (e.g. missing_reasoning) aren't
                kind = CONTEXT_TYPES[event.kind]  # context management
                extra["context_management"] = {"type": kind[0], "boundary": kind[1]}
            out_steps.append(
                {
                    "step_id": len(out_steps) + 1,
                    "timestamp": event.at,
                    "source": "system",
                    "message": event.message,
                    "extra": extra,
                }
            )

    flush(0)
    for record in steps:
        step: dict[str, Any] = {
            "step_id": len(out_steps) + 1,
            "timestamp": record.started_at,
            "source": "agent",
            "message": record.message,
            "observation": {
                "results": [{"source_call_id": record.call_id, "content": record.observation}]
            },
            "extra": {
                "repogym": {
                    "ok": record.ok,
                    "exit_code": record.exit_code,
                    "truncated": record.truncated,
                    "timed_out": record.timed_out,
                    "seconds": record.seconds,
                    "raw_chars": record.raw_chars,
                }
            },
        }
        if record.tool:
            step["tool_calls"] = [
                {
                    "tool_call_id": record.call_id,
                    "function_name": record.tool,
                    "arguments": record.arguments,
                }
            ]
        else:  # a turn with no valid action (e.g. a format error); the reply is the observation
            step["observation"]["results"][0].pop("source_call_id")
        if record.model_name:
            step["model_name"] = record.model_name
        if record.reasoning:
            step["reasoning_content"] = record.reasoning
        if record.metrics:
            step["metrics"] = record.metrics
            step["llm_call_count"] = record.llm_calls or 1
        else:  # a scripted dispatch, or an LLM reply that couldn't be used (no usage kept)
            step["llm_call_count"] = record.llm_calls if record.tool is None else 0
        out_steps.append(step)
        flush(record.step)
    flush(10**9)
    final: dict[str, Any] = {"total_steps": len(out_steps), "extra": final_extra}
    for key, value in (token_totals or {}).items():
        if value is not None:
            final[key] = value
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "trajectory_id": episode_id,
        "agent": agent,
        "steps": out_steps,
        "final_metrics": final,
        "extra": root_extra,
    }


# -- validator -------------------------------------------------------------------------------


class ATIFError(ValueError):
    pass


def _type(value: Any, types: tuple, where: str, errors: list[str], optional: bool = True) -> bool:
    if value is None:
        if not optional:
            errors.append(f"{where}: required")
        return False
    if isinstance(value, bool) and bool not in types:
        errors.append(f"{where}: must be {'/'.join(t.__name__ for t in types)}, got bool")
        return False
    if not isinstance(value, types):
        errors.append(
            f"{where}: must be {'/'.join(t.__name__ for t in types)}, got {type(value).__name__}"
        )
        return False
    return True


def _content(value: Any, where: str, errors: list[str]) -> None:
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        errors.append(f"{where}: must be a string or an array of content parts")
        return
    for i, part in enumerate(value):
        here = f"{where}[{i}]"
        if not isinstance(part, dict):
            errors.append(f"{here}: must be an object")
            continue
        kind = part.get("type")
        if kind not in ("text", "image", "audio"):
            errors.append(f"{here}.type: must be text, image or audio")
        elif kind == "text":
            if not isinstance(part.get("text"), str):
                errors.append(f"{here}.text: required for type text")
            if "source" in part:
                errors.append(f"{here}.source: must be omitted for type text")
        else:
            source = part.get("source")
            if "text" in part:
                errors.append(f"{here}.text: must be omitted for type {kind}")
            if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                errors.append(f"{here}.source: object with a path is required for type {kind}")
            elif source.get("media_type") not in (IMAGE_TYPES if kind == "image" else AUDIO_TYPES):
                errors.append(f"{here}.source.media_type: not a valid {kind} media type")


def validate(doc: Any, embedded: bool = False, where: str = "trajectory") -> list[str]:
    """All violations of the ATIF-v1.x rules in ``doc``; empty if valid."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"{where}: must be an object"]
    version = doc.get("schema_version")
    if not (isinstance(version, str) and version.startswith("ATIF-v1.")):
        errors.append(f"{where}.schema_version: must be a string like 'ATIF-v1.8'")
    _type(doc.get("session_id"), (str,), f"{where}.session_id", errors)
    _type(doc.get("trajectory_id"), (str,), f"{where}.trajectory_id", errors, optional=not embedded)
    _type(doc.get("notes"), (str,), f"{where}.notes", errors)
    _type(doc.get("extra"), (dict,), f"{where}.extra", errors)
    agent = doc.get("agent")
    if _type(agent, (dict,), f"{where}.agent", errors, optional=False):
        for key in ("name", "version"):
            _type(agent.get(key), (str,), f"{where}.agent.{key}", errors, optional=False)
        _type(agent.get("model_name"), (str,), f"{where}.agent.model_name", errors)
        if _type(agent.get("tool_definitions"), (list,), f"{where}.agent.tool_definitions", errors):
            for i, tool in enumerate(agent["tool_definitions"]):
                if not (
                    isinstance(tool, dict)
                    and tool.get("type") == "function"
                    and isinstance(tool.get("function"), dict)
                    and isinstance(tool["function"].get("name"), str)
                ):
                    errors.append(
                        f"{where}.agent.tool_definitions[{i}]: must be "
                        "{type: function, function: {name, ...}}"
                    )
    steps = doc.get("steps")
    call_ids: set[str] = set()
    if _type(steps, (list,), f"{where}.steps", errors, optional=False):
        for i, step in enumerate(steps):
            errors += _validate_step(step, i, f"{where}.steps[{i}]", call_ids)
    final = doc.get("final_metrics")
    if _type(final, (dict,), f"{where}.final_metrics", errors):
        for key in (
            "total_prompt_tokens",
            "total_completion_tokens",
            "total_cached_tokens",
            "total_steps",
        ):
            _type(final.get(key), (int,), f"{where}.final_metrics.{key}", errors)
        _type(
            final.get("total_cost_usd"),
            (int, float),
            f"{where}.final_metrics.total_cost_usd",
            errors,
        )
        _type(final.get("extra"), (dict,), f"{where}.final_metrics.extra", errors)
    _type(doc.get("continued_trajectory_ref"), (str,), f"{where}.continued_trajectory_ref", errors)
    subs = doc.get("subagent_trajectories")
    if _type(subs, (list,), f"{where}.subagent_trajectories", errors):
        seen = set()
        for i, sub in enumerate(subs):
            errors += validate(sub, embedded=True, where=f"{where}.subagent_trajectories[{i}]")
            tid = sub.get("trajectory_id") if isinstance(sub, dict) else None
            if tid in seen:
                errors.append(f"{where}.subagent_trajectories[{i}]: duplicate trajectory_id")
            seen.add(tid)
    return errors


def _validate_step(step: Any, index: int, where: str, call_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(step, dict):
        return [f"{where}: must be an object"]
    if step.get("step_id") != index + 1 or isinstance(step.get("step_id"), bool):
        errors.append(f"{where}.step_id: must be {index + 1} (sequential from 1)")
    source = step.get("source")
    if source not in SOURCES:
        errors.append(f"{where}.source: must be one of {sorted(SOURCES)}")
    if "message" not in step:
        errors.append(f"{where}.message: required (may be an empty string)")
    else:
        _content(step["message"], f"{where}.message", errors)
    _type(step.get("timestamp"), (str,), f"{where}.timestamp", errors)
    if source in ("system", "user"):
        for key in AGENT_ONLY:
            if key in step:
                errors.append(f"{where}.{key}: only allowed on agent steps")
    if source == "user" and "observation" in step:
        errors.append(f"{where}.observation: only allowed on agent and system steps")
    step_calls: set[str] = set()
    tool_calls = step.get("tool_calls")
    if _type(tool_calls, (list,), f"{where}.tool_calls", errors):
        for j, call in enumerate(tool_calls):
            here = f"{where}.tool_calls[{j}]"
            if not isinstance(call, dict):
                errors.append(f"{here}: must be an object")
                continue
            if _type(call.get("tool_call_id"), (str,), f"{here}.tool_call_id", errors, False):
                if call["tool_call_id"] in call_ids:
                    errors.append(f"{here}.tool_call_id: must be unique")
                call_ids.add(call["tool_call_id"])
                step_calls.add(call["tool_call_id"])
            _type(call.get("function_name"), (str,), f"{here}.function_name", errors, False)
            _type(call.get("arguments"), (dict,), f"{here}.arguments", errors, False)
    observation = step.get("observation")
    if _type(observation, (dict,), f"{where}.observation", errors):
        results = observation.get("results")
        if _type(results, (list,), f"{where}.observation.results", errors, False):
            for k, result in enumerate(results):
                here = f"{where}.observation.results[{k}]"
                if not isinstance(result, dict):
                    errors.append(f"{here}: must be an object")
                    continue
                ref = result.get("source_call_id")
                if ref is not None and ref not in step_calls:
                    errors.append(f"{here}.source_call_id: {ref!r} matches no tool_call_id")
                if "content" in result:
                    _content(result["content"], f"{here}.content", errors)
                elif "subagent_trajectory_ref" not in result:
                    errors.append(f"{here}: needs content or subagent_trajectory_ref")
                refs = result.get("subagent_trajectory_ref")
                if _type(refs, (list,), f"{here}.subagent_trajectory_ref", errors):
                    for r in refs:
                        if not isinstance(r, dict) or not (
                            r.get("trajectory_id") or r.get("trajectory_path")
                        ):
                            errors.append(
                                f"{here}.subagent_trajectory_ref: each needs "
                                "trajectory_id or trajectory_path"
                            )
    metrics = step.get("metrics")
    if _type(metrics, (dict,), f"{where}.metrics", errors):
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            _type(metrics.get(key), (int,), f"{where}.metrics.{key}", errors)
        _type(metrics.get("cost_usd"), (int, float), f"{where}.metrics.cost_usd", errors)
    count = step.get("llm_call_count")
    if _type(count, (int,), f"{where}.llm_call_count", errors) and count == 0:
        for key in ("metrics", "reasoning_content"):
            if key in step:
                errors.append(f"{where}.{key}: must be omitted when llm_call_count is 0")
    _type(step.get("is_copied_context"), (bool,), f"{where}.is_copied_context", errors)
    _type(step.get("reasoning_content"), (str,), f"{where}.reasoning_content", errors)
    _type(step.get("reasoning_effort"), (str, int, float), f"{where}.reasoning_effort", errors)
    _type(step.get("extra"), (dict,), f"{where}.extra", errors)
    return errors
