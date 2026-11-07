"""Blind human-labelling sheets for calibrating the task-type classifier and the spec check.

The sheets never show our own predictions (GitHub-label type, spec flags); those are kept in a
separate key file, so the labeller isn't anchored by them.
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from repogym.models import Candidate, VerifiedTask
from repogym.spec import added_text
from repogym.tasktype import type_from_labels

EXCERPT_CHARS = 800
TEST_EXCERPT_LINES = 40


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def links(candidate: Candidate) -> str:
    base = f"https://github.com/{candidate.repo}"
    return f"{base}/issues/{candidate.issue_number} {base}/pull/{candidate.pr_number}"


def _round_robin(groups: dict[str, list[str]], n: int, rng: random.Random) -> list[str]:
    """Take ``n`` items cycling through groups (shuffled), so every group is represented."""
    pools = {key: rng.sample(items, len(items)) for key, items in sorted(groups.items())}
    picked: list[str] = []
    while len(picked) < n and any(pools.values()):
        for key in sorted(pools):
            if pools[key] and len(picked) < n:
                picked.append(pools[key].pop())
    return picked


def select_task_type_sample(
    tasks: list[VerifiedTask], candidates: dict[str, Candidate], seed: int = 1
) -> list[tuple[str, str]]:
    """30 tasks: 10 GitHub-labelled bugs, up to 8 features, unknowns for the rest.

    Within each stratum, picks cycle across repos.
    """
    rng = random.Random(seed)
    strata: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for task in tasks:
        c = candidates[task.task_id]
        strata[type_from_labels(c.pr_labels + c.issue_labels)][task.repo].append(task.task_id)
    picked = [(t, "bug") for t in _round_robin(strata["bug"], 10, rng)]
    picked += [(t, "feature") for t in _round_robin(strata["feature"], 8, rng)]
    picked += [(t, "unknown") for t in _round_robin(strata["unknown"], 30 - len(picked), rng)]
    return picked


def select_spec_sample(tasks: list[VerifiedTask], n: int = 25, seed: int = 2) -> list[str]:
    """Every flagged task, plus a repo-stratified random sample of unflagged ones."""
    rng = random.Random(seed)
    flagged = sorted(t.task_id for t in tasks if not t.spec_clear)
    clear: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        if task.spec_clear:
            clear[task.repo].append(task.task_id)
    return flagged + _round_robin(clear, n - len(flagged), rng)


def write_task_type_sheet(
    path: Path, sample: list[tuple[str, str]], candidates: dict[str, Candidate], seed: int
) -> None:
    rng = random.Random(seed)
    rows = rng.sample(sample, len(sample))  # shuffle so strata aren't visible from the order
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task_id", "issue_title", "issue_body_excerpt", "links",
                         "my_label (bug / feature / other)", "notes"])  # fmt: skip
        for task_id, _ in rows:
            c = candidates[task_id]
            writer.writerow([task_id, c.issue_title, excerpt(c.issue_body), links(c), "", ""])
    key = {task_id: stratum for task_id, stratum in sample}
    path.with_suffix(".key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")


def test_patch_excerpt(test_patch: str, lines: int = TEST_EXCERPT_LINES) -> str:
    added = added_text(test_patch).splitlines()
    shown = "\n".join(added[:lines])
    return shown + (f"\n... ({len(added) - lines} more added lines)" if len(added) > lines else "")


test_patch_excerpt.__test__ = False  # type: ignore[attr-defined]


def write_spec_sheet(
    path: Path,
    sample: list[str],
    tasks: dict[str, VerifiedTask],
    candidates: dict[str, Candidate],
    seed: int,
) -> None:
    rng = random.Random(seed)
    rows = rng.sample(sample, len(sample))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task_id", "issue_title", "issue_body_excerpt", "test_patch_excerpt",
                         "links", "my_spec_clear (yes / no)", "notes"])  # fmt: skip
        for task_id in rows:
            c = candidates[task_id]
            writer.writerow([
                task_id, c.issue_title, excerpt(c.issue_body, 1500),
                test_patch_excerpt(tasks[task_id].test_patch), links(c), "", "",
            ])  # fmt: skip
    key = {
        task_id: {
            "spec_clear": tasks[task_id].spec_clear,
            "spec_issues": tasks[task_id].spec_issues,
        }
        for task_id in sample
    }
    path.with_suffix(".key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
