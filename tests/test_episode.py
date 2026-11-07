"""Budgets, truncation, tool argument errors, ATIF output and validation, and flags. No Docker."""

import copy
import json

import pytest

from repogym import trajectory
from repogym.env.budgets import Budgets, truncate
from repogym.env.environment import Action, Environment, StepRecord
from repogym.env.extract import AgentWork
from repogym.env.sandbox import ExecResult, _Capped
from repogym.flags import compute
from repogym.grade import Grade, Revert
from repogym.tools.definitions import validate_arguments
from tests.test_release import _task

# -- truncation ------------------------------------------------------------------------------


def test_truncation_keeps_head_and_tail_with_a_marker():
    text = "A" * 600 + "B" * 600
    out, cut = truncate(text, 400)
    assert cut and out.startswith("A" * 160) and out.endswith("B" * 160)
    assert "[... 880 characters truncated ...]" in out and len(out) <= 400 + 40
    assert truncate("short", 400) == ("short", False)


def test_capped_stream_keeps_head_and_tail_and_counts_everything():
    sink = _Capped(5)
    for chunk in (b"hello", b" big ", b"world", b"!!"):
        sink.feed(chunk)
    assert sink.total == 17
    assert sink.text() == "hello\n[... 7 bytes not captured ...]\nrld!!"
    small = _Capped(10)
    small.feed(b"abc")
    assert small.text() == "abc"


# -- tool argument validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("rm_rf", {}, "unknown tool 'rm_rf'"),
        ("bash", {}, "missing required argument(s): command"),
        ("bash", {"command": "ls", "cwd": "/"}, "unknown argument(s): cwd"),
        ("bash", {"command": 3}, "'command' must be a string"),
        ("view_file", {"path": "a", "start_line": 0}, "must be >= 1"),
        ("view_file", {"path": "a", "start_line": True}, "must be a integer"),
        ("submit", [], "arguments must be a JSON object"),
    ],
)
def test_tool_argument_errors_are_informative(tool, args, error):
    assert error in validate_arguments(tool, args)


def test_valid_arguments_pass():
    assert validate_arguments("view_file", {"path": "a", "start_line": 1, "end_line": 5}) is None
    assert validate_arguments("submit", {}) is None


# -- budgets, with a fake sandbox ------------------------------------------------------------


class FakeSandbox:
    def __init__(self, output="ok"):
        self.output, self.calls = output, []

    def exec_agent(self, argv, timeout, stdin=None, **kwargs):
        self.calls.append((argv, timeout))
        return ExecResult(0, self.output, "", False, 0.01, len(self.output), 0)


def env_with(budgets, output="ok"):
    env = Environment(
        _task("t", "2024-01-01T00:00:00Z", "dev").model_copy(update={"repo": "pallets/click"}),
        budgets,
    )
    env.sandbox = FakeSandbox(output)
    env._t0 = __import__("time").monotonic()
    env._tracked = {"tests/test_a.py"}
    return env


def test_step_budget_ends_the_episode():
    env = env_with(Budgets(max_steps=2))
    assert env.step(Action(tool="bash", arguments={"command": "ls"}))[1] is False
    obs, done, info = env.step(Action(tool="bash", arguments={"command": "ls"}))
    assert done and info["done_reason"] == "budget:steps"
    obs, done, _ = env.step(Action(tool="bash", arguments={"command": "ls"}))
    assert done and "over" in obs.text and len(env.steps) == 2


def test_observation_truncation_and_total_budget():
    env = env_with(Budgets(max_observation_chars=200, max_total_observation_chars=500), "x" * 5000)
    obs, done, _ = env.step(Action(tool="bash", arguments={"command": "cat big"}))
    assert obs.truncated and "characters truncated" in obs.text and len(obs.text) <= 240
    assert env.steps[0].raw_chars > 5000
    for _ in range(3):
        obs, done, info = env.step(Action(tool="bash", arguments={"command": "cat big"}))
    assert done and env.state().done_reason == "budget:observation_chars"


def test_wall_clock_budget_and_command_timeout_capping():
    env = env_with(Budgets(max_wall_seconds=50, command_timeout_seconds=120))
    env.step(Action(tool="bash", arguments={"command": "sleep 1"}))
    assert env.sandbox.calls[-1][1] <= 50  # a command never outlives the episode
    env._t0 -= 60
    obs, done, info = env.step(Action(tool="bash", arguments={"command": "ls"}))
    assert done and info["done_reason"] == "budget:wall_clock"


def test_run_tests_only_runs_tests_that_exist_at_base():
    env = env_with(Budgets())
    obs, _, _ = env.step(Action(tool="run_tests", arguments={"target": "tests/test_hidden.py"}))
    assert not obs.ok and "not a test file in the repository" in obs.text
    env.step(Action(tool="run_tests", arguments={"target": "tests/test_a.py::test_x"}))
    argv = env.sandbox.calls[-1][0]
    assert argv[:3] == ["python", "-m", "pytest"] and "--timeout=30" in argv
    assert argv[-1] == "tests/test_a.py::test_x"


def test_invalid_actions_are_recorded_and_count_as_steps():
    env = env_with(Budgets())
    obs, done, _ = env.step(Action(tool="nope", arguments={}))
    assert not obs.ok and not done and env.state().step_count == 1
    assert env.steps[0].tool == "nope" and "unknown tool" in env.steps[0].observation


# -- ATIF ------------------------------------------------------------------------------------


def records():
    return [
        StepRecord(
            step=1,
            call_id="call_1",
            started_at="2026-11-07T12:00:00.000Z",
            seconds=0.1,
            tool="bash",
            arguments={"command": "ls"},
            observation="[exit code 0]\na",
            ok=True,
            exit_code=0,
        ),
        StepRecord(
            step=2,
            call_id="turn_2",
            started_at="2026-11-07T12:00:01.000Z",
            seconds=0,
            tool=None,
            arguments={},
            observation="Format error",
            ok=False,
            message="I think...",
            model_name="m",
            metrics={"prompt_tokens": 10, "completion_tokens": 5},
            llm_calls=1,
        ),
        StepRecord(
            step=3,
            call_id="call_3",
            started_at="2026-11-07T12:00:02.000Z",
            seconds=0,
            tool="submit",
            arguments={},
            observation="Submitted.",
            ok=True,
        ),
    ]


def doc():
    return trajectory.build(
        episode_id="ep1",
        session_id="run1",
        agent_name="a",
        agent_version="1",
        model_name=None,
        system_text="sys",
        user_text="issue",
        started_at="2026-11-07T12:00:00.000Z",
        steps=records(),
        final_extra={"repogym": {"reward": 1}},
        root_extra={"repogym": {"task_id": "t"}},
        token_totals={"total_prompt_tokens": 10, "total_cost_usd": None},
    )


def test_built_trajectory_is_valid_atif_v18():
    d = doc()
    assert trajectory.validate(d) == []
    assert d["schema_version"] == "ATIF-v1.8"
    assert [s["source"] for s in d["steps"]] == ["system", "user", "agent", "agent", "agent"]
    call = d["steps"][2]
    assert (
        call["tool_calls"][0]["tool_call_id"] == call["observation"]["results"][0]["source_call_id"]
    )
    assert call["llm_call_count"] == 0 and "metrics" not in call  # scripted: deterministic
    assert "source_call_id" not in d["steps"][3]["observation"]["results"][0]
    assert d["final_metrics"]["total_steps"] == 5 and "total_cost_usd" not in d["final_metrics"]
    json.dumps(d)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d.pop("agent"), "agent: required"),
        (lambda d: d["steps"][2].update(step_id=7), "must be 3"),
        (lambda d: d["steps"][1].update(tool_calls=[]), "only allowed on agent steps"),
        (
            lambda d: d["steps"][2]["observation"]["results"][0].update(source_call_id="x"),
            "matches no tool_call_id",
        ),
        (
            lambda d: d["steps"][2].update(metrics={"prompt_tokens": 1}),
            "must be omitted when llm_call_count is 0",
        ),
        (lambda d: d["steps"][2].update(source="robot"), "must be one of"),
        (lambda d: d.update(schema_version="ATIF-2"), "schema_version"),
        (lambda d: d["steps"][2].pop("message"), "message: required"),
        (lambda d: d["steps"][3]["metrics"].update(prompt_tokens="10"), "must be int"),
        (
            lambda d: d["steps"][2].update(
                message=[{"type": "image", "source": {"path": "x", "media_type": "image/bmp"}}]
            ),
            "media type",
        ),
    ],
)
def test_validator_rejects_invalid_documents(mutate, error):
    d = copy.deepcopy(doc())
    mutate(d)
    assert any(error in e for e in trajectory.validate(d)), trajectory.validate(d)


# -- flags -----------------------------------------------------------------------------------


def step(n, command, observation=""):
    return StepRecord(
        step=n,
        call_id=f"c{n}",
        started_at="t",
        seconds=0,
        tool="bash",
        arguments={"command": command},
        observation=observation,
        ok=True,
    )


def test_flags_record_each_attempt_with_its_step():
    steps = [
        step(1, "git log --all -p | head"),
        step(2, "git -C /work reflog"),
        step(3, "curl -s https://github.com"),
        step(4, "pip install -U click"),
        step(5, "find / -name '*.patch'"),
        step(6, "ls /repos /data"),
        step(7, "python -m pytest tests"),
        step(8, "git status && git diff"),
    ]
    work = AgentWork(
        diff="", changed={}, committed=True, refs={}, head="x", outside_repo=["/tmp/x"]
    )
    g = Grade(
        status="graded",
        reward=0,
        reverts=[
            Revert(path="tests/t.py", kind="test_file", action="checkout"),
            Revert(path="pytest.py", kind="shadowing_module", action="delete"),
        ],
    )
    flags = compute(steps, work, g, "budget:steps", ["secret gold line here!!"])
    assert [e.step for e in flags.git_history_commands] == [1, 2]
    assert [e.step for e in flags.network_attempts] == [3, 4]
    assert [e.step for e in flags.task_data_search] == [5, 6]
    assert flags.test_files_edited[0].detail.startswith("test_file: tests/t.py")
    assert flags.non_test_reverts[0].detail.startswith("shadowing_module: pytest.py")
    assert flags.agent_committed and flags.files_outside_repo and flags.budget_exhausted
    assert not flags.gold_content_in_observations


def test_gold_content_in_observations_is_detected_except_for_the_oracle():
    steps = [step(1, "cat x", observation="... secret gold line here!! ...")]
    assert compute(
        steps, None, None, None, ["secret gold line here!!"]
    ).gold_content_in_observations
    assert not compute(
        steps, None, None, None, ["secret gold line here!!"], is_oracle=True
    ).gold_content_in_observations


def test_ordinary_work_raises_no_flags():
    steps = [
        step(1, "grep -rn answer src/"),
        step(2, "python -m pytest -q tests/test_a.py"),
        step(3, "git diff"),
    ]
    assert (
        compute(
            steps, AgentWork("", {}, False, {}, "b"), Grade(status="graded"), "submitted", []
        ).raised()
        == []
    )
