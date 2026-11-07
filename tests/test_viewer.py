"""The viewer's data endpoints (no server, no network)."""

import json

from repogym.viewer import PAGE, episode, list_runs, run_episodes


def write_run(root, run_id, records):
    run = root / run_id
    (run / "trajectories").mkdir(parents=True)
    (run / "diffs").mkdir()
    with (run / "episodes.jsonl").open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
            (run / "trajectories" / f"{record['task_id']}.json").write_text(json.dumps(
                {"schema_version": "ATIF-v1.8", "steps": [{"step_id": 1}]}))
            (run / "diffs" / f"{record['task_id']}.diff").write_text("+fix\n")


def rec(task_id, reward, status="graded"):
    return {"task_id": task_id, "agent": "repogym-oracle", "agent_version": "1", "reward": reward,
            "status": status, "steps": 2, "flags_raised": [], "flags": {}, "done_reason": "submitted",
            "error": "", "trajectory": f"trajectories/{task_id}.json", "grade": {}}


def test_runs_episodes_and_replay_payload(tmp_path):
    write_run(tmp_path, "r1", [rec("a", 1), rec("b", 0), rec("a", 1)])  # a rerun keeps the latest
    write_run(tmp_path, "r2", [rec("c", None, "setup_error")])
    runs = {r["run_id"]: r for r in list_runs(tmp_path)}
    assert runs["r1"]["episodes"] == 2 and runs["r1"]["solved"] == 1 and runs["r1"]["graded"] == 2
    assert runs["r2"]["graded"] == 0
    assert [e["task_id"] for e in run_episodes(tmp_path, "r1")] == ["a", "b"]
    shown = episode(tmp_path, "r1", "a")
    assert shown["trajectory"]["schema_version"] == "ATIF-v1.8" and shown["diff"] == "+fix\n"


def test_page_renders_agent_content_as_text_only():
    # Trajectories contain agent output; the page must never insert it as HTML.
    assert "innerHTML" not in PAGE and "createTextNode" in PAGE
