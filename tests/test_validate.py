import pytest

from repogym import validate as v
from repogym.env.docker import DockerUnavailable, _parse_run
from repogym.models import Candidate, Rejection, RunStage, TestRun, VerifiedTask

P, F, E = "passed", "failed", "error"


def run(tests=None, collection_errors=None, stage=RunStage.COMPLETED):
    return TestRun(stage=stage, tests=tests or {}, collection_errors=collection_errors or [])


def test_lists_basic():
    lists = v.compute_lists(
        run({"m::keep": P}),
        run({"m::new": F, "m::keep": P}),
        run({"m::new": P, "m::keep": P}),
    )
    assert lists.fail_to_pass == ["m::new"]
    assert lists.pass_to_pass == ["m::keep"]


def test_baseline_failures_enter_neither_list():
    lists = v.compute_lists(
        run({"m::env_broken": F, "m::env_err": E, "m::passes_later": F}),
        run({"m::env_broken": F, "m::env_err": E, "m::passes_later": P}),
        run({"m::env_broken": P, "m::env_err": P, "m::passes_later": P}),
    )
    assert lists.fail_to_pass == []
    assert lists.pass_to_pass == []


def test_baseline_collection_error_excludes_the_whole_module():
    lists = v.compute_lists(
        run(collection_errors=["tests.test_a"]),
        run({"tests.test_a::t": F, "tests.test_b::t": F}),
        run({"tests.test_a::t": P, "tests.test_b::t": P}),
    )
    assert lists.fail_to_pass == ["tests.test_b::t"]


def test_import_error_before_the_fix_counts_as_failing():
    lists = v.compute_lists(
        run(), run(collection_errors=["tests.test_a"]), run({"tests.test_a::t": P})
    )
    assert lists.fail_to_pass == ["tests.test_a::t"]


def test_test_absent_before_fix_without_import_error_is_ignored():
    lists = v.compute_lists(run(), run({"m::a": F}), run({"m::a": P, "m::generated": P}))
    assert lists.fail_to_pass == ["m::a"]
    assert lists.pass_to_pass == []


def test_tests_failing_with_the_fix_enter_neither_list():
    lists = v.compute_lists(run(), run({"m::a": F, "m::b": P}), run({"m::a": F, "m::b": F}))
    assert lists.fail_to_pass == [] and lists.pass_to_pass == []


def test_inconsistent_tests_finds_status_changes_and_missing_tests():
    runs = [run({"a": P, "b": P, "c": P}), run({"a": P, "b": F, "c": P}), run({"a": P, "c": P})]
    assert v.inconsistent_tests(runs) == ["b"]


def _candidate():
    return Candidate(
        task_id="pallets__click-1",
        repo="pallets/click",
        pr_number=1,
        issue_number=2,
        issue_title="t",
        issue_body="b",
        link_source="closing_keyword",
        base_commit="abc",
        gold_patch="GOLD",
        test_patch="TEST",
        created_at="2024-01-01T00:00:00Z",
        changed_lines=5,
        source_files=["src/click/a.py"],
        test_files=["tests/test_a.py"],
    )


def scripted(baseline, unsolved, oracle, noop=None, oracle_repeats=None):
    """A fake container runner, dispatching on which patches are applied."""
    repeats = iter(oracle_repeats or [oracle, oracle])
    oracle_calls = iter([oracle, *repeats])
    unsolved_calls = iter([unsolved, noop or unsolved])

    def runner(spec, base, patch, test_patch, files, **kwargs):
        if patch == "GOLD":
            return next(oracle_calls)
        return next(unsolved_calls) if test_patch == "TEST" else baseline

    return runner


GOOD_ORACLE = run({"m::new": P, "m::keep": P})
GOOD_UNSOLVED = run({"m::new": F, "m::keep": P})


def test_good_candidate_is_verified():
    outcome = v.validate_candidate(_candidate(), scripted(run(), GOOD_UNSOLVED, GOOD_ORACLE))
    assert outcome.rejection is None
    assert outcome.verified.fail_to_pass == ["m::new"]
    assert outcome.verified.pass_to_pass == ["m::keep"]
    assert outcome.verified.docker_image == "repogym/click:base"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"unsolved": run({"m::new": P}), "oracle": run({"m::new": P})},
            "check1_nothing_fails_without_fix",
        ),
        (
            {"unsolved": run({"m::new": F}), "oracle": run({"m::new": F})},
            "check2_oracle_does_not_fix",
        ),
        (
            {
                "unsolved": GOOD_UNSOLVED,
                "oracle": GOOD_ORACLE,
                "noop": run({"m::new": P, "m::keep": P}),
            },
            "check3_noop_counts_as_solved",
        ),
        (
            {
                "unsolved": run({"m::a": F, "m::b": F}),
                "oracle": run({"m::a": P, "m::b": P}),
                "noop": run({"m::a": P, "m::b": F}),
            },
            "flaky_unsolved_state",
        ),
        (
            {
                "unsolved": GOOD_UNSOLVED,
                "oracle": GOOD_ORACLE,
                "oracle_repeats": [GOOD_ORACLE, run({"m::new": F, "m::keep": P})],
            },
            "flaky_oracle",
        ),
        (
            {"unsolved": GOOD_UNSOLVED, "oracle": run(stage=RunStage.PATCH_FAILED)},
            "gold_patch_apply_failed",
        ),
        (
            {"unsolved": GOOD_UNSOLVED, "oracle": run(stage=RunStage.TIMEOUT)},
            "oracle_timeout",
        ),
    ],
)
def test_rejections(kwargs, reason):
    outcome = v.validate_candidate(_candidate(), scripted(run(), **kwargs))
    assert outcome.verified is None
    assert outcome.rejection.reason == reason


def test_baseline_only_failures_give_a_distinct_reason():
    outcome = v.validate_candidate(
        _candidate(), scripted(run({"m::a": F}), run({"m::a": F}), run({"m::a": P}))
    )
    assert outcome.rejection.reason == "all_fixed_tests_fail_at_baseline"
    assert outcome.baseline.failing == ["m::a"]


def test_env_failure_at_baseline_is_rejected_not_raised():
    outcome = v.validate_candidate(
        _candidate(), scripted(run(stage=RunStage.INSTALL_FAILED), GOOD_UNSOLVED, GOOD_ORACLE)
    )
    assert outcome.rejection.reason == "env_install_failed"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "VERIFIED_PATH", tmp_path / "v.jsonl")
    monkeypatch.setattr(v, "REJECTIONS_PATH", tmp_path / "r.jsonl")
    monkeypatch.setattr(v, "BASELINE_PATH", tmp_path / "b.jsonl")
    monkeypatch.setattr(v, "RUN_METRICS_PATH", tmp_path / "m.jsonl")
    return tmp_path


def test_rerun_skips_finished_tasks(stores):
    calls = []

    def runner(spec, base, patch, test_patch, files, **kwargs):
        calls.append(patch)
        if patch == "GOLD":
            return GOOD_ORACLE
        return GOOD_UNSOLVED if test_patch else run()

    assert v.validate_all([_candidate()], 1, runner)["verified"] == 1
    first = len(calls)
    assert v.validate_all([_candidate()], 1, runner)["verified"] == 0
    assert len(calls) == first


def test_docker_failure_records_nothing_so_rerun_retries(stores):
    def broken(*args, **kwargs):
        raise DockerUnavailable("daemon gone")

    with pytest.raises(DockerUnavailable):
        v.validate_all([_candidate()], 1, broken)
    assert v.already_validated() == set()


def test_truncated_last_line_is_skipped_and_next_append_is_clean(stores):
    good = Rejection(task_id="a", repo="r", reason="x")
    path = stores / "r.jsonl"
    path.write_text(good.model_dump_json() + '\n{"task_id": "b", "re')
    store = v.JsonlStore(path)
    store.append(Rejection(task_id="c", repo="r", reason="y"))
    assert [r.task_id for r in v.read_jsonl(path, Rejection)] == ["a", "c"]


def test_forget_rejections_removes_only_given_reasons(stores):
    path = stores / "r.jsonl"
    store = v.JsonlStore(path)
    store.append(Rejection(task_id="a", repo="r", reason="oracle_timeout"))
    store.append(Rejection(task_id="b", repo="r", reason="flaky_oracle"))
    assert v.forget_rejections(path, {"oracle_timeout"}) == 1
    assert [r.task_id for r in v.read_jsonl(path, Rejection)] == ["b"]


def test_daemon_error_raises_rather_than_rejecting():
    with pytest.raises(DockerUnavailable):
        _parse_run(125, "", "docker: Error response from daemon: boom")


def test_verified_schema_has_exactly_the_requested_fields():
    assert set(VerifiedTask.model_fields) == {
        "task_id", "repo", "base_commit", "issue_title", "issue_body", "gold_patch",
        "test_patch", "fail_to_pass", "pass_to_pass", "docker_image",
    }  # fmt: skip
