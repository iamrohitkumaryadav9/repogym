"""A thin adapter to the OpenEnv environment contract (openenv-core 0.3.0), with no dependency.

OpenEnv's base package pulls in gradio, fastmcp, huggingface_hub, openai and FastAPI, so it isn't
a RepoGym dependency. Instead these models mirror its types field for field (``Action``,
``Observation``, ``State``, and the MCP tool-call types ``ListToolsAction``, ``CallToolAction``,
``ListToolsObservation``, ``CallToolObservation``, ``Tool``, ``ToolError``). Their dumps validate
against OpenEnv's own models; that conformance check runs locally, outside CI, and is described
in docs/design/week2_environment.md §15.

An OpenEnv episode is a normal RepoGym harness episode: the same setup and leakage checks,
budgets, grading, flags and ATIF trajectory. ``reset`` sets it up, each ``CallToolAction`` is one
``Environment.step``, and the observation for ``submit`` carries the graded reward.
"""

from __future__ import annotations

import threading
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from repogym.env.budgets import Budgets
from repogym.tools.definitions import BY_NAME, TOOLS, validate_arguments
from repogym.tools.mcp_server import BridgeAgent

_STRICT = ConfigDict(extra="forbid", validate_assignment=True, arbitrary_types_allowed=True)


class Action(BaseModel):
    model_config = _STRICT
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListToolsAction(Action):
    type: Literal["list_tools"] = "list_tools"


class CallToolAction(Action):
    type: Literal["call_tool"] = "call_tool"
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    model_config = _STRICT
    done: bool = False
    reward: bool | int | float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Tool(BaseModel):
    model_config = _STRICT
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolErrorType(StrEnum):
    EXECUTION_ERROR = "execution_error"
    INVALID_ARGS = "invalid_args"
    TRANSPORT_ERROR = "transport_error"
    TOOL_NOT_FOUND = "tool_not_found"
    TIMEOUT = "timeout"


class ToolError(BaseModel):
    model_config = _STRICT
    error_type: ToolErrorType
    message: str


class ListToolsObservation(Observation):
    tools: list[Tool] = Field(default_factory=list)


class CallToolObservation(Observation):
    tool_name: str
    result: Any = None
    error: ToolError | None = None


class State(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True, arbitrary_types_allowed=True)
    episode_id: str | None = None
    step_count: int = 0


def tools() -> list[Tool]:
    return [
        Tool(name=t["name"], description=t["description"], input_schema=t["parameters"])
        for t in TOOLS
    ]


class RepoGymOpenEnv:
    """OpenEnv-style environment over RepoGym tasks: ``reset(task_id=...)``, ``step``, ``state``."""

    SUPPORTS_CONCURRENT_SESSIONS = True  # every episode has its own container
    REQUIRES_SINGLE_THREAD_EXECUTOR = False

    def __init__(
        self, run_dir: Path | None = None, budgets: Budgets | None = None, runner=None
    ) -> None:
        from repogym.harness import RUNS_DIR, run_episode

        self.run_dir = run_dir or RUNS_DIR / "openenv"
        self.budgets = budgets or Budgets()
        self._runner = runner or run_episode
        self._bridge: BridgeAgent | None = None
        self._worker: threading.Thread | None = None
        self._outcome: dict = {}
        self._state = State()

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> Observation:
        """Start an episode on ``task_id`` (setup and every leakage check run first).
        ``seed`` is recorded but unused: the environment itself is deterministic."""
        from repogym.env.environment import load_task

        if task_id is None:
            raise ValueError("reset needs task_id=<a task id from the release>")
        self.close()
        task = load_task(task_id)
        bridge, outcome = BridgeAgent(name=kwargs.get("agent_name", "openenv-client")), {}

        def episode() -> None:
            try:
                outcome["record"] = self._runner(
                    task, bridge, self.run_dir, self.budgets, seed=seed
                )
            finally:
                bridge.ready.set()
                bridge.finished.set()

        self._bridge, self._outcome = bridge, outcome
        self._worker = threading.Thread(target=episode, daemon=True)
        self._worker.start()
        bridge.ready.wait()
        if bridge.first is None:
            record = outcome.get("record")
            raise RuntimeError(f"episode setup failed: {getattr(record, 'error', 'unknown')}")
        self._state = State(episode_id=episode_id, step_count=0, task_id=task_id, done=False)
        return Observation(
            metadata={
                "prompt": bridge.first.text,
                "task_id": task_id,
                "tools": [t.name for t in tools()],
            }
        )

    def step(self, action: Action, timeout_s: float | None = None, **kwargs: Any) -> Observation:
        """One action. ``timeout_s`` is ignored: RepoGym's own budgets bound every command."""
        if isinstance(action, ListToolsAction):
            return ListToolsObservation(tools=tools())
        if not isinstance(action, CallToolAction):
            raise TypeError(f"unsupported action {type(action).__name__}")
        if self._bridge is None or self._state.done:
            return CallToolObservation(
                tool_name=action.tool_name,
                done=True,
                error=ToolError(
                    error_type=ToolErrorType.EXECUTION_ERROR,
                    message="no active episode; call reset",
                ),
            )
        if action.tool_name not in BY_NAME:
            return CallToolObservation(
                tool_name=action.tool_name,
                error=ToolError(
                    error_type=ToolErrorType.TOOL_NOT_FOUND,
                    message=f"unknown tool {action.tool_name!r}; available: {', '.join(BY_NAME)}",
                ),
            )
        problem = validate_arguments(action.tool_name, action.arguments)
        if problem:
            return CallToolObservation(
                tool_name=action.tool_name,
                error=ToolError(error_type=ToolErrorType.INVALID_ARGS, message=problem),
            )
        observation = self._bridge.step(action.tool_name, action.arguments)
        self._state.step_count += 1
        out = CallToolObservation(
            tool_name=action.tool_name, result=observation.text, done=observation.done
        )
        if not observation.ok:
            kind = (
                ToolErrorType.TIMEOUT
                if "timeout]" in observation.text
                else ToolErrorType.EXECUTION_ERROR
            )
            out.error = ToolError(error_type=kind, message=observation.text[:2000])
        if observation.done:
            self._state.done = True
            if self._worker is not None:
                self._worker.join(timeout=1800)
            record = self._outcome.get("record")
            out.reward = getattr(record, "reward", None)
            out.metadata = {
                "status": getattr(record, "status", None),
                "flags_raised": getattr(record, "flags_raised", []),
                "done_reason": getattr(record, "done_reason", None),
            }
        return out

    @property
    def state(self) -> State:
        return self._state

    def close(self) -> None:
        if self._bridge is not None and not self._state.done and self._worker is not None:
            self._bridge.actions.put(None)  # end the episode; it's still graded and recorded
            self._worker.join(timeout=1800)
        self._bridge = None
