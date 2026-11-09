"""The tool agent goes through the same environment path as the scripted agents.

A fake provider replays the oracle's edits as function calls. The resulting episode must have
the same provenance, flags, setup checks and grading path as an Oracle episode on the same task:
same container image, same grading, same anti-cheating checks, and nothing done outside
``env.step``. Run with ``pytest -m docker``.
"""

import json

import pytest

from repogym import trajectory
from repogym.agents.scripted import OracleAgent, final_files
from repogym.agents.tool_agent import ToolAgent
from repogym.context.base import ContextConfig
from repogym.context.strategies import ContextManager
from repogym.context.tokens import TokenEstimator
from repogym.env.budgets import Budgets
from repogym.experiments.spec import release_tasks
from repogym.harness import run_episode
from repogym.providers.base import Attempt, Generation, Message, ToolCall, Usage

pytestmark = pytest.mark.docker
TASK = "python-attrs__attrs-1448"  # dev split
BUDGETS = Budgets(max_steps=75, max_observation_chars=200_000)
SAME = (
    "task_id",
    "status",
    "reward",
    "flags",
    "flags_raised",
    "done_reason",
    "release",
    "tasks_sha256",
    "image",
    "image_digest",
    "repogym_commit",
    "repogym_dirty",
    "budgets",
    "setup_checks",
    "history_recoverable",
    "termination",
)


class ReplayProvider:
    """Emits the oracle's actions as function calls, one per reply, then submit."""

    name = "replay-model"

    def __init__(self, task):
        files, deleted = final_files(task)
        self.script = [("create_file", {"path": p, "content": c}) for p, c in sorted(files.items())]
        self.script += [("bash", {"command": f"rm -f -- '{p}'"}) for p in deleted]
        self.script.append(("submit", {}))

    def generate(self, system, messages, tools, estimated_tokens=0):
        tool, args = self.script.pop(0)
        call = ToolCall(id=f"c{len(self.script)}", name=tool, args=args)
        return Generation(
            message=Message(role="assistant", calls=[call], raw={"role": "model"}),
            usage=Usage(prompt_tokens=100, output_tokens=10),
            finish_reason="STOP",
            model_version=self.name,
            latency_seconds=0.0,
            http_status=200,
            attempts=[Attempt(200, "ok")],
        )

    def generate_text(self, *args, **kwargs):
        raise AssertionError("no summaries in this test")

    def count_tokens(self, *args, **kwargs):
        return None


def strip_timing(value):
    """Drop measurements that vary run to run (durations, timestamps, peak memory)."""
    if isinstance(value, dict):
        return {
            k: strip_timing(v)
            for k, v in value.items()
            if not any(t in k for t in ("second", "time", "_at", "duration", "memory"))
        }
    if isinstance(value, list):
        return [strip_timing(v) for v in value]
    return value


def test_tool_agent_episode_matches_an_oracle_episode(tmp_path):
    task = release_tasks()[TASK]
    oracle = run_episode(task, OracleAgent(task), tmp_path / "oracle", BUDGETS)
    context = ContextManager("obs_mask", ContextConfig(budget=10**7), TokenEstimator())
    agent = ToolAgent(ReplayProvider(task), context)
    tool = run_episode(task, agent, tmp_path / "tool", BUDGETS, episode_key="k", experiment={})

    a, b = oracle.model_dump(mode="json"), tool.model_dump(mode="json")
    assert {k: a[k] for k in SAME} == {k: b[k] for k in SAME}
    assert a["reward"] == 1 and a["termination"] == "submitted"
    assert strip_timing(a["grade"]) == strip_timing(b["grade"])  # same grading path and result
    assert a["steps"] == b["steps"]  # one env.step per oracle action, nothing extra
    assert b["agent"] == "repogym-tool-agent" and b["model_name"] == "replay-model"
    assert b["accounting"]["calls"] == b["steps"]

    for run in ("oracle", "tool"):
        diffs = sorted((tmp_path / run).glob("diffs/*.diff"))
        assert len(diffs) == 1
    assert (
        sorted((tmp_path / "oracle").glob("diffs/*.diff"))[0].read_text()
        == sorted((tmp_path / "tool").glob("diffs/*.diff"))[0].read_text()
    )
    traj = json.loads((tmp_path / "tool" / tool.trajectory).read_text())
    assert trajectory.validate(traj) == []
