"""Assemble a release from the verified tasks, candidates and provenance."""

from __future__ import annotations

import subprocess

from repogym.config import CANDIDATES_PATH, PROVENANCE_PATH, RELEASE_DIR, ROOT, VERIFIED_PATH
from repogym.models import Candidate, Provenance, VerifiedTask
from repogym.release import ReleaseTask, assign_splits, write_release
from repogym.validate import read_jsonl


def build_release(version: str) -> dict:
    tasks = read_jsonl(VERIFIED_PATH, VerifiedTask)
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    provenance = {p.task_id: p for p in read_jsonl(PROVENANCE_PATH, Provenance)}
    missing = [t.task_id for t in tasks if t.task_id not in provenance]
    if missing:
        raise RuntimeError(f"{len(missing)} verified tasks lack provenance, e.g. {missing[:3]}")
    image_ids = {provenance[t.task_id].docker_image: provenance[t.task_id].image_id for t in tasks}
    commits = {provenance[t.task_id].repogym_commit for t in tasks}

    split = assign_splits({t.task_id: candidates[t.task_id].merged_at for t in tasks})
    released = []
    for task in tasks:
        c = candidates[task.task_id]
        base = f"https://github.com/{c.repo}"
        released.append(
            ReleaseTask(
                **task.model_dump(),
                split=split.splits[task.task_id],
                merged_at=c.merged_at,
                created_at=c.created_at,
                pr_url=f"{base}/pull/{c.pr_number}",
                issue_url=f"{base}/issues/{c.issue_number}",
            )
        )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    manifest = write_release(
        RELEASE_DIR / version, version, released, split, image_ids, repogym_commit=head
    )
    manifest["validated_with_commits"] = sorted(commits)
    return manifest
