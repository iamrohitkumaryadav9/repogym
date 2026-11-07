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
    task_type: str
    spec_clear: bool
    spec_issues: list[str]


@dataclass(frozen=True)
class SplitResult:
    splits: dict[str, str]  # task_id -> train | dev | test
    dev_start: str | None  # earliest merge time in dev
    test_start: str | None  # earliest merge time in test


def assign_splits(
    merged_at: dict[str, str],
    test_fraction: float = TEST_FRACTION,
    dev_fraction: float = DEV_FRACTION,
) -> SplitResult:
    """Most recent ``test_fraction`` by merge time is test, the next ``dev_fraction`` dev.

    Ties are broken by task id, so the split is deterministic. Splitting by time, not at random,
    means a model is never evaluated on fixes older than ones it was tuned on.
    """
    ordered = sorted(merged_at, key=lambda task: (merged_at[task], task))
    n = len(ordered)
    n_test = round(n * test_fraction)
    n_dev = round(n * dev_fraction)
    test = ordered[n - n_test :] if n_test else []
    dev = ordered[n - n_test - n_dev : n - n_test] if n_dev else []
    splits = {task: "train" for task in ordered}
    splits.update({task: "dev" for task in dev})
    splits.update({task: "test" for task in test})
    return SplitResult(
        splits=splits,
        dev_start=merged_at[dev[0]] if dev else None,
        test_start=merged_at[test[0]] if test else None,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_release(
    out_dir: Path,
    version: str,
    tasks: list[ReleaseTask],
    split: SplitResult,
    image_ids: dict[str, str],
    repogym_commit: str,
) -> dict:
    """Write tasks.jsonl and manifest.json. Refuses to overwrite an existing release."""
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} exists; releases are immutable")
    out_dir.mkdir(parents=True)
    ordered = sorted(tasks, key=lambda t: (t.merged_at, t.task_id))
    tasks_path = out_dir / "tasks.jsonl"
    tasks_path.write_text("".join(t.model_dump_json() + "\n" for t in ordered), encoding="utf-8")

    per_split: dict[str, Counter[str]] = {}
    for task in ordered:
        per_split.setdefault(task.split, Counter())[task.repo] += 1
    manifest = {
        "version": version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_count": len(ordered),
        "per_repo": dict(sorted(Counter(t.repo for t in ordered).items())),
        "tasks_sha256": sha256_file(tasks_path),
        "repogym_commit": repogym_commit,
        "docker_images": {
            image: image_ids[image] for image in sorted({t.docker_image for t in ordered})
        },
        "splits": {
            "method": "by PR merge time: newest 20% test, next 10% dev, rest train",
            "dev_start": split.dev_start,
            "test_start": split.test_start,
            "counts": {name: sum(c.values()) for name, c in sorted(per_split.items())},
            "per_repo": {name: dict(sorted(c.items())) for name, c in sorted(per_split.items())},
        },
        "merged_at_range": [ordered[0].merged_at, ordered[-1].merged_at] if ordered else None,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
