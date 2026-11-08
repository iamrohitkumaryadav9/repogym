"""The OpenEnv adapter: contract shapes and an episode through a fake runner (no Docker)."""

import pytest

from repogym.env.environment import Observation as EnvObservation
from repogym.openenv_adapter import (
    CallToolAction,
    CallToolObservation,
    ListToolsAction,
    ListToolsObservation,
    Observation,
    RepoGymOpenEnv,
    State,
    ToolErrorType,
)


class FakeRecord:
    reward, status, flags_raised, done_reason, error = 1, "graded", [], "submitted", ""


def fake_runner(task, agent, run_dir, budgets, seed=None):
    class Env:
        def step(self, action):
            done = action.tool == "submit"
            ok = action.tool != "view_file"
            return EnvObservation(text=f"ran {action.tool}", ok=ok, done=done), done, {}

    agent.run(Env(), EnvObservation(text="the task prompt"))
    return FakeRecord()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("repogym.env.environment.load_task", lambda task_id: object())
    return RepoGymOpenEnv(run_dir=tmp_path, runner=fake_runner)


def test_reset_step_state_contract(env):
    first = env.reset(task_id="t1", episode_id="ep-1", seed=7)
    assert isinstance(first, Observation) and not first.done and first.reward is None
    assert first.metadata["prompt"] == "the task prompt" and "submit" in first.metadata["tools"]
    assert env.state == State(episode_id="ep-1", step_count=0, task_id="t1", done=False)

    listed = env.step(ListToolsAction())
    assert isinstance(listed, ListToolsObservation)
    assert [t.name for t in listed.tools] == [
        "bash",
        "view_file",
        "str_replace",
        "create_file",
        "run_tests",
        "submit",
    ]
    assert listed.tools[0].input_schema["required"] == ["command"]

    out = env.step(CallToolAction(tool_name="bash", arguments={"command": "ls"}))
    assert isinstance(out, CallToolObservation) and out.result == "ran bash" and out.error is None
    failed = env.step(CallToolAction(tool_name="view_file", arguments={"path": "x"}))
    assert failed.error.error_type is ToolErrorType.EXECUTION_ERROR
    assert env.state.step_count == 2

    final = env.step(CallToolAction(tool_name="submit"))
    assert final.done and final.reward == 1 and final.metadata["status"] == "graded"
    assert env.state.done


def test_bad_actions_get_typed_errors_and_do_not_count(env):
    env.reset(task_id="t1")
    unknown = env.step(CallToolAction(tool_name="rm_rf"))
    assert unknown.error.error_type is ToolErrorType.TOOL_NOT_FOUND
    bad = env.step(CallToolAction(tool_name="bash", arguments={"cmd": "ls"}))
    assert bad.error.error_type is ToolErrorType.INVALID_ARGS
    assert env.state.step_count == 0
    env.close()


def test_models_are_strict_like_openenv():
    with pytest.raises(ValueError):
        Observation(unexpected=1)
    with pytest.raises(ValueError):
        CallToolAction(tool_name="bash", type="list_tools")
    assert State(episode_id="e", extra_field=1).extra_field == 1  # State allows extras


def test_reset_needs_a_task(env):
    with pytest.raises(ValueError, match="task_id"):
        env.reset()
