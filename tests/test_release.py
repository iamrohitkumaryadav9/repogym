import json

import pytest

from repogym.release import (
    ReleaseTask,
    assign_splits,
    assign_splits_per_repo,
    sha256_file,
    split_counts,
    validate_release,
    write_release,
)
from repogym.sheets import select_spec_sample, select_task_type_sample


def _daily(n, start_day=1):
    """One task per day: t000 on 2024-01-01, t001 on 2024-01-02, ..."""
    from datetime import date, timedelta

    first = date(2024, 1, start_day)
    return {f"t{i:03d}": f"{first + timedelta(days=i)}T12:00:00Z" for i in range(n)}


def _by_split(result):
    out = {"train": [], "dev": [], "test": []}
    for task, split in result.splits.items():
        out[split].append(task)
    return {k: sorted(v) for k, v in out.items()}


def test_newest_twenty_percent_is_test_next_ten_percent_dev():
    dates = _daily(100)
    result = assign_splits(dates)
    by_split = _by_split(result)
    assert [len(by_split[s]) for s in ("train", "dev", "test")] == [70, 10, 20]
    assert max(dates[t] for t in by_split["train"]) < min(dates[t] for t in by_split["dev"])
    assert max(dates[t] for t in by_split["dev"]) < min(dates[t] for t in by_split["test"])
    assert result.test_start == min(dates[t] for t in by_split["test"])
    assert result.test_cutoff == result.test_start[:10] and result.dev_cutoff == "2024-03-11"


def test_boundary_is_an_inclusive_utc_date():
    dates = _daily(10)  # test target 2 -> the last two days, 2024-01-09 and 2024-01-10
    dates["midnight"] = "2024-01-09T00:00:00Z"  # first second of the cutoff date -> test
    dates["late"] = "2024-01-08T23:59:59Z"  # last second before it -> not test
    result = assign_splits(dates)
    assert result.test_cutoff == "2024-01-09"
    assert result.splits["midnight"] == "test" and result.splits["late"] != "test"
    assert all((s == "test") == (dates[t][:10] >= "2024-01-09") for t, s in result.splits.items())


def test_tasks_merged_on_the_same_date_are_never_split():
    dates = _daily(18)
    # Five tasks on the date where a per-task cut would fall (the 20% boundary).
    for i in range(5):
        dates[f"same{i}"] = f"2024-01-17T0{i}:00:00Z"
    result = assign_splits(dates)
    assert len({result.splits[f"same{i}"] for i in range(5)} | {result.splits["t016"]}) == 1
    by_day = {}
    for task, split in result.splits.items():
        by_day.setdefault(dates[task][:10], set()).add(split)
    assert all(len(splits) == 1 for splits in by_day.values())


def test_cut_is_the_day_boundary_closest_to_the_target():
    # 20 tasks: 10% dev, 20% test -> targets 2 and 4. Newest day holds 3, the day before 3.
    dates = _daily(14)
    dates |= {f"n{i}": f"2024-02-01T0{i}:00:00Z" for i in range(3)}
    dates |= {f"m{i}": f"2024-01-31T0{i}:00:00Z" for i in range(3)}
    result = assign_splits(dates)
    sizes = {k: len(v) for k, v in _by_split(result).items()}
    # Test: 3 (one day, gap 1) beats 6 (two days, gap 2). Dev: the next day has 3 (gap 1)
    # versus none (gap 2), so dev takes it.
    assert sizes == {"train": 14, "dev": 3, "test": 3}


def test_splits_ignore_input_order():
    dates = _daily(30)
    dates |= {"x": "2024-01-30T01:00:00Z", "y": "2024-01-30T02:00:00Z"}
    first = assign_splits(dates).splits
    assert first == assign_splits(dict(reversed(list(dates.items())))).splits


def test_per_repo_counts_per_split():
    dates = _daily(10)
    repos = {t: ("r/a" if i % 2 else "r/b") for i, t in enumerate(sorted(dates))}
    counts = split_counts(assign_splits(dates).splits, repos)
    assert counts == {
        "dev": {"r/a": 1},
        "test": {"r/a": 1, "r/b": 1},
        "train": {"r/a": 3, "r/b": 4},
    }
    assert sum(sum(c.values()) for c in counts.values()) == 10


def test_small_inputs():
    assert assign_splits({}).splits == {}
    assert assign_splits({"only": "2024-01-01T00:00:00Z"}).splits == {"only": "train"}
    result = assign_splits({"only": "2024-01-01T00:00:00Z"})
    assert result.test_cutoff is None and result.dev_cutoff is None


def _task(task_id, merged, split, repo="pallets/click"):
    return ReleaseTask(
        task_id=task_id, repo=repo, split=split, split_global=split, merged_at=merged,
        created_at=merged, pr_url="u", issue_url="u", base_commit="abc", issue_title="t",
        issue_body="b", gold_patch="g", test_patch="t", fail_to_pass=["m::a"], pass_to_pass=[],
        docker_image="repogym/click:base", task_type="bug", task_type_source="github",
        spec_clear=True, spec_clear_source="heuristic", spec_check_clear=True, spec_issues=[],
    )  # fmt: skip


def _release(tmp_path, tasks, name="v", **kwargs):
    """Assign both splits the way release_build does, then write the release."""
    merged = {t.task_id: t.merged_at for t in tasks}
    primary = assign_splits_per_repo(merged, {t.task_id: t.repo for t in tasks})
    global_split = assign_splits(merged)
    tasks = [t.model_copy(update={"split": primary.splits[t.task_id],
                                  "split_global": global_split.splits[t.task_id]})
             for t in tasks]  # fmt: skip
    out = tmp_path / name
    manifest = write_release(out, name, tasks, primary, global_split,
                             {"repogym/click:base": "sha256:1"}, "c0ffee", **kwargs)  # fmt: skip
    return out, manifest


def test_release_writes_tasks_and_a_manifest(tmp_path):
    tasks = [
        _task("b", "2024-02-01T00:00:00Z", "test"),
        _task("a", "2024-01-01T00:00:00Z", "train"),
    ]
    out, manifest = _release(tmp_path, tasks, "v0.1")
    lines = (out / "tasks.jsonl").read_text().splitlines()
    assert [json.loads(line)["task_id"] for line in lines] == ["a", "b"]  # sorted by merge time
    assert manifest["task_count"] == 2
    assert manifest["tasks_sha256"] == sha256_file(out / "tasks.jsonl")
    assert manifest["docker_images"] == {"repogym/click:base": "sha256:1"}
    assert manifest["repogym_commit"] == "c0ffee"
    assert json.loads((out / "manifest.json").read_text()) == manifest
    assert validate_release(out) == []


def _two_repos():
    # click: 20 tasks over 20 days; attrs: 10 older tasks, so a global cut leaves attrs out of test.
    tasks = [_task(f"c{i:02d}", f"2024-02-{i + 1:02d}T00:00:00Z", "", "pallets/click")
             for i in range(20)]  # fmt: skip
    tasks += [_task(f"a{i:02d}", f"2024-01-{i + 1:02d}T00:00:00Z", "", "python-attrs/attrs")
              for i in range(10)]  # fmt: skip
    return tasks


def test_manifest_records_both_split_views_cutoffs_and_labels(tmp_path):
    out, manifest = _release(tmp_path, _two_repos(),
                             extra={"classifier": {"models": ["m"], "prompt_version": "p"}})  # fmt: skip
    primary, global_view = manifest["splits"]["primary"], manifest["splits"]["global"]
    assert primary["per_repo_cutoffs"] == {
        "pallets/click": {"dev_cutoff_date": "2024-02-15", "test_cutoff_date": "2024-02-17"},
        "python-attrs/attrs": {"dev_cutoff_date": "2024-01-08", "test_cutoff_date": "2024-01-09"},
    }
    assert primary["per_repo"]["test"] == {"pallets/click": 4, "python-attrs/attrs": 2}
    assert primary["per_repo"]["dev"] == {"pallets/click": 2, "python-attrs/attrs": 1}
    # The global view is the old behaviour: attrs is entirely older than the global cutoffs.
    assert global_view["test_cutoff_date"] == "2024-02-15"
    assert "python-attrs/attrs" not in global_view["per_repo"]["test"]
    assert manifest["labels"]["task_type_source"] == {"github": 30}
    assert manifest["classifier"]["prompt_version"] == "p"
    assert validate_release(out) == []


def test_validation_catches_a_tampered_tasks_file(tmp_path):
    out, _ = _release(tmp_path, _two_repos())
    path = out / "tasks.jsonl"
    path.write_text(path.read_text().replace('"split":"train"', '"split":"test"', 1))
    problems = validate_release(out)
    assert any("sha256" in p for p in problems) and any("repo's cutoffs" in p for p in problems)


def test_releases_are_never_overwritten(tmp_path):
    out = tmp_path / "v0.1"
    out.mkdir()
    with pytest.raises(FileExistsError):
        write_release(out, "v0.1", [], assign_splits_per_repo({}, {}), assign_splits({}), {}, "c")


# -- per-repo split ---------------------------------------------------------------------------


def _repo_dates(spec):
    """{repo: n_tasks} -> merged_at and repos, one task per day per repo, newest repo last."""
    from datetime import date, timedelta

    merged, repos = {}, {}
    for r, (repo, n) in enumerate(spec.items()):
        for i in range(n):
            task = f"{repo}-{i:03d}"
            merged[task] = f"{date(2024, 1, 1) + timedelta(days=100 * r + i)}T12:00:00Z"
            repos[task] = repo
    return merged, repos


def test_per_repo_split_balances_every_repo():
    merged, repos = _repo_dates({"r/a": 70, "r/b": 50, "r/c": 20, "r/d": 10})
    result = assign_splits_per_repo(merged, repos)
    counts = split_counts(result.splits, repos)
    assert counts["test"] == {"r/a": 14, "r/b": 10, "r/c": 4, "r/d": 2}
    assert counts["dev"] == {"r/a": 7, "r/b": 5, "r/c": 2, "r/d": 1}
    # Within each repo, train < dev < test in time.
    for repo in ("r/a", "r/b", "r/c", "r/d"):
        mine = {t: s for t, s in result.splits.items() if repos[t] == repo}
        last = {s: max(merged[t] for t, x in mine.items() if x == s) for s in set(mine.values())}
        first = {s: min(merged[t] for t, x in mine.items() if x == s) for s in set(mine.values())}
        assert last["train"] < first["dev"] and last["dev"] < first["test"]


def test_per_repo_boundaries_are_that_repos_inclusive_dates():
    merged, repos = _repo_dates({"r/a": 10, "r/b": 10})
    merged["r/a-extra"], repos["r/a-extra"] = "2024-01-09T00:00:00Z", "r/a"  # cutoff day, 00:00
    result = assign_splits_per_repo(merged, repos)
    cut = result.by_repo["r/a"]
    assert cut.test_cutoff == "2024-01-09" and result.splits["r/a-extra"] == "test"
    assert result.by_repo["r/b"].test_cutoff != cut.test_cutoff  # each repo has its own dates


def test_same_day_tasks_in_a_repo_are_never_separated():
    merged, repos = _repo_dates({"r/a": 12})
    for i in range(4):  # four extra tasks on the day a per-task cut would split
        merged[f"same{i}"], repos[f"same{i}"] = f"2024-01-10T0{i}:00:00Z", "r/a"
    result = assign_splits_per_repo(merged, repos)
    assert len({result.splits[f"same{i}"] for i in range(4)} | {result.splits["r/a-009"]}) == 1


def test_repo_with_enough_tasks_appears_in_every_split_even_when_rounding_says_zero():
    # 10 tasks: dev target is 1.0; make the newest pre-test day hold 3 tasks so "closest" is 0.
    merged, repos = _repo_dates({"r/a": 8})
    for i in range(3):
        merged[f"busy{i}"], repos[f"busy{i}"] = f"2024-01-07T0{i}:00:00Z", "r/a"
    merged.pop("r/a-006")
    result = assign_splits_per_repo(merged, repos)
    assert set(result.splits.values()) == {"train", "dev", "test"}


def test_small_repos():
    # Below the minimum, a repo follows the plain closest-boundary rule and may skip dev or test.
    merged, repos = _repo_dates({"r/one": 1, "r/two": 2, "r/four": 4})
    result = assign_splits_per_repo(merged, repos)
    assert result.splits["r/one-000"] == "train"
    assert result.by_repo["r/one"].test_cutoff is None
    two = sorted(s for t, s in result.splits.items() if repos[t] == "r/two")
    assert two == ["train", "train"]  # targets 0.4 and 0.2 round to nothing
    four = [result.splits[f"r/four-{i:03d}"] for i in range(4)]
    assert four == ["train", "train", "train", "test"]
    assert assign_splits_per_repo({}, {}).splits == {}


def test_a_repo_whose_tasks_share_one_day_is_all_train():
    merged = {f"t{i}": f"2024-01-01T{i:02d}:00:00Z" for i in range(12)}
    repos = dict.fromkeys(merged, "r/a")
    assert set(assign_splits_per_repo(merged, repos).splits.values()) == {"train"}


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
