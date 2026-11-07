import json

import pytest

from repogym.release import ReleaseTask, assign_splits, sha256_file, write_release
from repogym.sheets import select_spec_sample, select_task_type_sample


def _dates(n):
    return {f"t{i:03d}": f"2024-01-{1 + i // 24:02d}T{i % 24:02d}:00:00Z" for i in range(n)}


def test_newest_twenty_percent_is_test_next_ten_percent_dev():
    dates = _dates(100)
    result = assign_splits(dates)
    by_split = {
        s: sorted(t for t, v in result.splits.items() if v == s) for s in ("train", "dev", "test")
    }
    assert [len(by_split[s]) for s in ("train", "dev", "test")] == [70, 10, 20]
    assert max(dates[t] for t in by_split["train"]) < min(dates[t] for t in by_split["dev"])
    assert max(dates[t] for t in by_split["dev"]) < min(dates[t] for t in by_split["test"])
    assert result.test_start == min(dates[t] for t in by_split["test"])
    assert result.dev_start == min(dates[t] for t in by_split["dev"])


def test_splits_ignore_input_order_and_break_ties_by_id():
    dates = {"b": "2024-01-01T00:00:00Z", "a": "2024-01-01T00:00:00Z", "c": "2023-01-01T00:00:00Z"}
    first = assign_splits(dates, test_fraction=1 / 3, dev_fraction=0).splits
    second = assign_splits(dict(reversed(list(dates.items()))), 1 / 3, 0).splits
    assert first == second and first["b"] == "test" and first["a"] == "train"


def test_small_inputs():
    assert assign_splits({}).splits == {}
    assert assign_splits({"only": "2024-01-01T00:00:00Z"}).splits == {"only": "train"}


def _task(task_id, merged, split, repo="pallets/click"):
    return ReleaseTask(
        task_id=task_id, repo=repo, split=split, merged_at=merged, created_at=merged,
        pr_url="u", issue_url="u", base_commit="abc", issue_title="t", issue_body="b",
        gold_patch="g", test_patch="t", fail_to_pass=["m::a"], pass_to_pass=[],
        docker_image="repogym/click:base", task_type="bug", spec_clear=True, spec_issues=[],
    )  # fmt: skip


def test_release_writes_tasks_and_a_manifest(tmp_path):
    tasks = [
        _task("b", "2024-02-01T00:00:00Z", "test"),
        _task("a", "2024-01-01T00:00:00Z", "train"),
    ]
    split = assign_splits({t.task_id: t.merged_at for t in tasks}, 0.5, 0)
    out = tmp_path / "v0.1"
    manifest = write_release(
        out, "v0.1", tasks, split, {"repogym/click:base": "sha256:1"}, "c0ffee"
    )
    lines = (out / "tasks.jsonl").read_text().splitlines()
    assert [json.loads(line)["task_id"] for line in lines] == ["a", "b"]  # sorted by merge time
    assert manifest["task_count"] == 2
    assert manifest["tasks_sha256"] == sha256_file(out / "tasks.jsonl")
    assert manifest["docker_images"] == {"repogym/click:base": "sha256:1"}
    assert manifest["repogym_commit"] == "c0ffee"
    assert manifest["splits"]["counts"] == {"test": 1, "train": 1}
    assert json.loads((out / "manifest.json").read_text()) == manifest


def test_releases_are_never_overwritten(tmp_path):
    out = tmp_path / "v0.1"
    out.mkdir()
    with pytest.raises(FileExistsError):
        write_release(out, "v0.1", [], assign_splits({}), {}, "c0ffee")


def test_spec_sample_includes_every_flagged_task_and_every_repo():
    from repogym.models import VerifiedTask

    def verified(task_id, repo, clear):
        return VerifiedTask(
            task_id=task_id, repo=repo, base_commit="a", issue_title="t", issue_body="b",
            gold_patch="g", test_patch="t", fail_to_pass=["x"], pass_to_pass=[],
            docker_image="i", spec_clear=clear,
        )  # fmt: skip

    tasks = [verified(f"f{i}", "r/a", False) for i in range(3)]
    tasks += [verified(f"{repo}-{i}", f"r/{repo}", True) for repo in "abc" for i in range(10)]
    sample = select_spec_sample(tasks, n=10)
    assert len(sample) == 10 and {"f0", "f1", "f2"} <= set(sample)
    assert {t.split("-")[0] for t in sample if "-" in t} == {"a", "b", "c"}
    assert sample == select_spec_sample(tasks, n=10)  # deterministic


def test_task_type_sample_strata():
    from repogym.models import VerifiedTask
    from tests.test_validate import _candidate

    tasks, candidates = [], {}
    for i, labels in enumerate([["bug"]] * 15 + [["Feature"]] * 4 + [[]] * 40):
        task_id = f"t{i}"
        candidates[task_id] = _candidate().model_copy(
            update={"task_id": task_id, "pr_labels": labels}
        )
        tasks.append(VerifiedTask(
            task_id=task_id, repo="pallets/click", base_commit="a", issue_title="t",
            issue_body="b", gold_patch="g", test_patch="t", fail_to_pass=["x"], pass_to_pass=[],
            docker_image="i",
        ))  # fmt: skip
    sample = select_task_type_sample(tasks, candidates)
    strata = [s for _, s in sample]
    assert len(sample) == 30 and len({t for t, _ in sample}) == 30
    assert (strata.count("bug"), strata.count("feature"), strata.count("unknown")) == (10, 4, 16)
