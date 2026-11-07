"""Three-run consensus labelling: decision rule, resumability and stability. No network."""

import json

from repogym.classify import TaskTypeLabel, cache_key
from repogym.consensus import classify_runs, decide, latest_runs, stability
from tests.test_classify import client, gemini_response
from tests.test_validate import _candidate


def lab(task_type, confidence=0.95, run=0, task="t", error=""):
    return TaskTypeLabel(task_id=task, task_type=task_type, confidence=confidence, run=run,
                         error=error, labelled_at="2026-11-07T00:00:00+00:00")  # fmt: skip


def answer(task_type, confidence=0.95):
    return json.dumps({"task_type": task_type, "confidence": confidence, "rationale": "r"})


def test_applied_only_when_all_three_agree_with_high_confidence():
    assert decide("t", [lab("bug", run=i) for i in range(3)]).task_type == "bug"
    assert decide("t", [lab("bug", 0.9, run=i) for i in range(3)]).outcome == "applied"


def test_disagreement_is_unknown():
    runs = [lab("bug", run=0), lab("feature", run=1), lab("bug", run=2)]
    result = decide("t", runs)
    assert (result.outcome, result.task_type) == ("disagreement", "unknown")


def test_one_low_confidence_run_is_unknown():
    runs = [lab("bug", run=0), lab("bug", 0.89, run=1), lab("bug", run=2)]
    assert decide("t", runs).outcome == "low_confidence"


def test_failed_or_missing_runs_are_unknown():
    failed = [lab("bug", run=0), lab("unknown", None, run=1), lab("bug", run=2)]
    assert decide("t", failed).outcome == "failed"
    assert decide("t", [lab("bug", run=0)]).outcome == "incomplete"


def test_each_run_has_its_own_cache_key_and_single_run_key_is_unchanged():
    c = _candidate()
    keys = {cache_key(c, run) for run in (0, 1, 2)}
    assert len(keys) == 3 and cache_key(c) not in keys
    assert cache_key(c, None).startswith("task-type-v1\n") and "run=" not in cache_key(c)


def test_runs_are_separate_requests_and_resume_retries_only_failures(tmp_path):
    saved = []
    c = client(tmp_path, {"primary-m": [answer("bug"), gemini_response("", 400), answer("bug")],
                          "fallback-m": []})  # fmt: skip
    done = {}
    assert classify_runs([_candidate()], c, done, saved.append)
    assert [r.run for r in saved] == [0, 1, 2] and saved[1].task_type == "unknown"
    assert decide("pallets__click-1", list(done["pallets__click-1"].values())).outcome == "failed"

    # A new session: rebuild state from the saved file; only run 1 is requested again.
    c2 = client(tmp_path, {"primary-m": [answer("bug")], "fallback-m": []})
    done2 = latest_runs(saved)
    assert classify_runs([_candidate()], c2, done2, saved.append)
    assert c2._transport.calls == ["primary-m"]
    result = decide("pallets__click-1", list(done2["pallets__click-1"].values()))
    assert (result.outcome, result.task_type) == ("applied", "bug")


def test_stops_early_when_retries_keep_being_exhausted(tmp_path):
    exhausted = [gemini_response("", 429)] * 5
    c = client(tmp_path, {"primary-m": exhausted * 3, "fallback-m": exhausted * 3})
    saved = []
    many = [_candidate().model_copy(update={"task_id": f"x-{i}"}) for i in range(3)]
    assert classify_runs(many, c, {}, saved.append, stop_after_unavailable=3) is False
    assert len(saved) == 3 and {s.task_id for s in saved} == {"x-0"}


def test_stability_counts_tasks_and_pairs():
    results = [
        decide("a", [lab("bug", run=i, task="a") for i in range(3)]),
        decide(
            "b",
            [
                lab("bug", run=0, task="b"),
                lab("feature", run=1, task="b"),
                lab("bug", run=2, task="b"),
            ],
        ),  # fmt: skip
        decide("c", [lab("bug", run=0, task="c")]),  # incomplete: excluded
    ]
    s = stability(results)
    assert (s.tasks, s.all_agree, s.pairs, s.pairs_agree) == (2, 1, 6, 4)
