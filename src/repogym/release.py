"""Time-based train/dev/test splits and frozen dataset releases."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

TEST_FRACTION = 0.2
DEV_FRACTION = 0.1


class ReleaseTask(BaseModel):
    """A verified task as shipped in a release: the task plus where and when it came from."""

    task_id: str
    repo: str
    split: str
    merged_at: str
    created_at: str
    pr_url: str
    issue_url: str
    base_commit: str
    issue_title: str
    issue_body: str
    gold_patch: str
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    docker_image: str
    task_type: str  # bug | feature | other | unknown
    task_type_source: str  # human | github | classifier | unknown
    spec_clear: bool
    spec_clear_source: str  # human | heuristic
    spec_check_clear: bool  # the heuristic check's own verdict, kept even when a human overrides
    spec_issues: list[str]  # names the heuristic check flagged


@dataclass(frozen=True)
class SplitResult:
    splits: dict[str, str]  # task_id -> train | dev | test
    dev_start: str | None  # earliest merge time in dev
    test_start: str | None  # earliest merge time in test
    dev_cutoff: str | None = None  # dev = merged on/after this UTC date and before test_cutoff
    test_cutoff: str | None = None  # test = merged on/after this UTC date


def merge_day(merged_at: str) -> str:
    """UTC calendar date of an ISO-8601 merge time (GitHub gives UTC, ``...Z``)."""
    return merged_at[:10]


def _cut(day_counts: list[tuple[str, int]], target: float) -> int:
    """How many of the newest days to take so their task count is closest to ``target``.

    Ties go to the smaller split. Whole days only, so a date is never split.
    """
    best, best_gap, total = 0, target, 0
    for taken, (_, count) in enumerate(reversed(day_counts), start=1):
        total += count
        gap = abs(total - target)
        if gap < best_gap:
            best, best_gap = taken, gap
        if total >= target:
            break
    return best


def assign_splits(
    merged_at: dict[str, str],
    test_fraction: float = TEST_FRACTION,
    dev_fraction: float = DEV_FRACTION,
) -> SplitResult:
    """Newest ~``test_fraction`` of tasks by merge date is test, the next ~``dev_fraction`` dev.

    Splits are cut between UTC calendar days, so every task merged on a given date is in the same
    split and each split is "merged on or after <date>". The cut is the day boundary that comes
    closest to the target fraction. Splitting by time, not at random, means a model is never
    evaluated on fixes older than ones it was tuned on.
    """
    n = len(merged_at)
    days = Counter(merge_day(m) for m in merged_at.values())
    day_counts = sorted(days.items())
    n_test_days = _cut(day_counts, n * test_fraction)
    remaining = day_counts[: len(day_counts) - n_test_days]
    n_dev_days = _cut(remaining, n * dev_fraction)
    test_days = {d for d, _ in day_counts[len(day_counts) - n_test_days :]}
    dev_days = {d for d, _ in remaining[len(remaining) - n_dev_days :]} if n_dev_days else set()

    splits = {}
    for task, merged in merged_at.items():
        day = merge_day(merged)
        splits[task] = "test" if day in test_days else "dev" if day in dev_days else "train"

    def first(name: str) -> str | None:
        times = [merged_at[t] for t, s in splits.items() if s == name]
        return min(times) if times else None

    return SplitResult(
        splits=splits,
        dev_start=first("dev"),
        test_start=first("test"),
        dev_cutoff=min(dev_days) if dev_days else None,
        test_cutoff=min(test_days) if test_days else None,
    )


def split_counts(splits: dict[str, str], repos: dict[str, str]) -> dict[str, dict[str, int]]:
    """{split: {repo: count}} for every split present."""
    counts: dict[str, Counter[str]] = {}
    for task, name in splits.items():
        counts.setdefault(name, Counter())[repos[task]] += 1
    return {name: dict(sorted(c.items())) for name, c in sorted(counts.items())}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    version: str,
    tasks: list[ReleaseTask],
    split: SplitResult,
    image_ids: dict[str, str],
    repogym_commit: str,
    tasks_sha256: str,
    extra: dict | None = None,
) -> dict:
    ordered = sorted(tasks, key=lambda t: (t.merged_at, t.task_id))
    per_split = split_counts({t.task_id: t.split for t in ordered},
                             {t.task_id: t.repo for t in ordered})  # fmt: skip

    def counts(field: str) -> dict[str, int]:
        return dict(sorted(Counter(getattr(t, field) for t in ordered).items()))

    return {
        "version": version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_count": len(ordered),
        "per_repo": counts("repo"),
        "tasks_file": "tasks.jsonl",
        "tasks_sha256": tasks_sha256,
        "repogym_commit": repogym_commit,
        "docker_images": {
            image: image_ids[image] for image in sorted({t.docker_image for t in ordered})
        },
        "splits": {
            "method": "by PR merge date (UTC days, never split): newest ~20% test, next ~10% dev, "
            "rest train",
            "test_cutoff_date": split.test_cutoff,
            "dev_cutoff_date": split.dev_cutoff,
            "dev_start": split.dev_start,
            "test_start": split.test_start,
            "counts": {name: sum(c.values()) for name, c in per_split.items()},
            "per_repo": per_split,
        },
        "merged_at_range": [ordered[0].merged_at, ordered[-1].merged_at] if ordered else None,
        "labels": {
            "task_type": counts("task_type"),
            "task_type_source": counts("task_type_source"),
            "task_type_by_source": {
                source: dict(
                    sorted(
                        Counter(
                            t.task_type for t in ordered if t.task_type_source == source
                        ).items()
                    )
                )
                for source in sorted({t.task_type_source for t in ordered})
            },  # fmt: skip
            "spec_clear": {str(k).lower(): v for k, v in counts("spec_clear").items()},
            "spec_clear_source": counts("spec_clear_source"),
        },
        **(extra or {}),
    }


def validate_release(out_dir: Path) -> list[str]:
    """Re-read a written release and check it against its manifest. Returns problems found."""
    problems = []
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    tasks_path = out_dir / manifest["tasks_file"]
    if sha256_file(tasks_path) != manifest["tasks_sha256"]:
        problems.append("tasks.jsonl sha256 does not match the manifest")
    tasks = [ReleaseTask.model_validate_json(line)
             for line in tasks_path.read_text(encoding="utf-8").splitlines()]  # fmt: skip
    ids = [t.task_id for t in tasks]
    if len(ids) != len(set(ids)):
        problems.append("duplicate task ids")
    if len(tasks) != manifest["task_count"]:
        problems.append("task count does not match the manifest")
    if dict(sorted(Counter(t.repo for t in tasks).items())) != manifest["per_repo"]:
        problems.append("per-repo counts do not match the manifest")
    if any(t.docker_image not in manifest["docker_images"] for t in tasks):
        problems.append("a task uses an image missing from the manifest")
    if any(not d.startswith("sha256:") for d in manifest["docker_images"].values()):
        problems.append("an image digest is missing")
    test_cutoff, dev_cutoff = (
        manifest["splits"]["test_cutoff_date"],
        manifest["splits"]["dev_cutoff_date"],
    )
    for t in tasks:
        day = merge_day(t.merged_at)
        expected = ("test" if test_cutoff and day >= test_cutoff
                    else "dev" if dev_cutoff and day >= dev_cutoff else "train")  # fmt: skip
        if t.split != expected:
            problems.append(f"{t.task_id}: split {t.split} disagrees with the cutoff dates")
        if not t.fail_to_pass:
            problems.append(f"{t.task_id}: empty FAIL_TO_PASS")
        if set(t.fail_to_pass) & set(t.pass_to_pass):
            problems.append(f"{t.task_id}: a test is in both FAIL_TO_PASS and PASS_TO_PASS")
        if (t.task_type == "unknown") != (t.task_type_source == "unknown"):
            problems.append(f"{t.task_id}: task_type and task_type_source disagree")
    return problems


def write_release(
    out_dir: Path,
    version: str,
    tasks: list[ReleaseTask],
    split: SplitResult,
    image_ids: dict[str, str],
    repogym_commit: str,
    extra: dict | None = None,
) -> dict:
    """Write tasks.jsonl and manifest.json. Refuses to overwrite an existing release."""
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} exists; releases are immutable")
    out_dir.mkdir(parents=True)
    ordered = sorted(tasks, key=lambda t: (t.merged_at, t.task_id))
    tasks_path = out_dir / "tasks.jsonl"
    tasks_path.write_text("".join(t.model_dump_json() + "\n" for t in ordered), encoding="utf-8")
    manifest = build_manifest(
        version, ordered, split, image_ids, repogym_commit, sha256_file(tasks_path), extra
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
