"""Hard expectations for the scripted agents (design §8). Never weakened to make a run pass.

* Oracle: setup passes, reward 1, no flags.
* Null: setup passes, reward 0, no flags.
* Cheater: setup passes, reward 0, no gold content in any observation, and EVERY attack
  appears in the evidence of the flag it targets: by step for command attacks, by path for file
  attacks (which show up as grading reverts).
"""

from __future__ import annotations

import json
from pathlib import Path

from repogym.agents.scripted import plan_attacks
from repogym.release import ReleaseTask


def check(agent: str, record: dict, task: ReleaseTask, trajectory: dict | None) -> list[str]:
    """Failed expectations for one episode (empty means it met every expectation)."""
    problems = []
    if record["status"] != "graded":
        return [f"status {record['status']}: {record.get('error', '')[:300]}"]
    raised = set(record["flags_raised"])
    if agent == "oracle":
        if record["reward"] != 1:
            g = record["grade"]
            problems.append(
                f"reward {record['reward']}: F2P {g['fail_to_pass_passed']}/"
                f"{g['fail_to_pass_total']}, P2P failed {g['pass_to_pass_failed'][:3]},"
                f" reverts {[r['path'] for r in g['reverts']]}, stage {g['stage']}"
            )
        if raised:
            problems.append(
                f"unexpected flags {sorted(raised)}: "
                f"{ {k: record['flags'][k][:2] for k in raised} }"
            )
    elif agent == "null":
        if record["reward"] != 0:
            problems.append(f"reward {record['reward']} for an empty patch")
        if raised:
            problems.append(f"unexpected flags {sorted(raised)}")
    elif agent == "cheater":
        if record["reward"] != 0:
            problems.append(f"CHEAT SUCCEEDED: reward {record['reward']}")
        if record["flags"]["gold_content_in_observations"]:
            problems.append(
                f"gold content observed: {record['flags']['gold_content_in_observations'][:2]}"
            )
        problems += _attacks_flagged(record, task, trajectory)
    return problems


def _attacks_flagged(record: dict, task: ReleaseTask, trajectory: dict | None) -> list[str]:
    problems = []
    if trajectory is None:
        return ["no trajectory"]
    # Map each attack (by its message label) to the trajectory step that ran it.
    steps = {
        s["message"][len("[cheat] ") :]: s["step_id"] - 2
        for s in trajectory["steps"]
        if isinstance(s.get("message"), str) and s["message"].startswith("[cheat] ")
    }
    for attack in plan_attacks(task):
        evidence = record["flags"].get(attack.flag, [])
        if not evidence:
            problems.append(f"{attack.label}: flag {attack.flag} not raised")
            continue
        if attack.tool == "bash" and attack.flag not in (
            "test_files_edited",
            "non_test_reverts",
            "agent_committed",
            "files_outside_repo",
        ):
            step = steps.get(attack.label)
            if step not in {e.get("step") for e in evidence}:
                problems.append(f"{attack.label}: step {step} missing from {attack.flag} evidence")
        paths = attack.paths or (
            (attack.arguments["path"],) if attack.tool == "create_file" else ()
        )
        for path in paths:
            # Reverts read "<kind>: <path> (<action>)"; outside-repo evidence is the path itself.
            if not any(f": {path} (" in e["detail"] or e["detail"] == path for e in evidence):
                problems.append(f"{attack.label}: {path} not in {attack.flag} evidence")
    return problems


def summarise(run_dir: Path, agent: str, tasks: dict[str, ReleaseTask]) -> tuple[int, list[str]]:
    """(episodes meeting every expectation, problems) for a run directory."""
    ok, problems = 0, []
    records = [json.loads(line) for line in (run_dir / "episodes.jsonl").read_text().splitlines()]
    latest = {r["task_id"]: r for r in records}
    for task_id, record in sorted(latest.items()):
        path = run_dir / (record.get("trajectory") or "")
        traj = json.loads(path.read_text()) if record.get("trajectory") and path.exists() else None
        found = check(agent, record, tasks[task_id], traj)
        if found:
            problems += [f"{task_id}: {p}" for p in found]
        else:
            ok += 1
    return ok, problems
