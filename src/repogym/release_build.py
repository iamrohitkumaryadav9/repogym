"""Assemble a release from the verified tasks, candidates, provenance and final labels."""

from __future__ import annotations

import csv
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from repogym.classify import PROMPT_VERSION, TaskTypeLabel
from repogym.config import (
    CANDIDATES_PATH,
    PROVENANCE_PATH,
    RELEASE_DIR,
    ROOT,
    SPEC_SHEET,
    TASK_TYPE_RUNS_PATH,
    TASK_TYPE_SHEET,
    VERIFIED_PATH,
    get_repo,
)
from repogym.consensus import MIN_CONFIDENCE, RUNS, decide, latest_runs
from repogym.labels import final_label, human_labels
from repogym.licenses import license_at
from repogym.models import Candidate, Provenance, VerifiedTask
from repogym.release import (
    ReleaseTask,
    assign_splits,
    assign_splits_per_repo,
    sha256_file,
    validate_release,
    write_release,
)
from repogym.validate import read_jsonl


class ReleaseError(RuntimeError):
    pass


def _adjudicated(spec_human: dict[str, str]) -> dict[str, str]:
    path = SPEC_SHEET.with_name("spec_adjudication.csv")
    if not path.exists():
        return spec_human
    with path.open(newline="", encoding="utf-8-sig") as handle:
        overrides = {r["task_id"]: r["adjudicated_spec_clear"] for r in csv.DictReader(handle)}
    return {**spec_human, **{t: v for t, v in overrides.items() if t in spec_human}}


RELEASE_INPUTS = ("src", "pyproject.toml", "uv.lock", "data/tasks_verified.jsonl",
                  "data/tasks_candidates.jsonl", "data/provenance.jsonl",
                  "data/task_type_runs.jsonl", "data/labelling")  # fmt: skip


def uncommitted_inputs() -> str:
    """Code or label inputs with uncommitted changes: a release must match its commit."""
    return subprocess.run(
        ["git", "status", "--porcelain", "--", *RELEASE_INPUTS],
        cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout.strip()  # fmt: skip


# Code that decides whether a task is verified and what its test lists are.
VALIDATION_CODE = ("src/repogym/validate.py", "src/repogym/env", "src/repogym/spec.py",
                   "src/repogym/tasktype.py", "src/repogym/mining/patches.py")  # fmt: skip

VALIDATED_DATA = (
    "data/tasks_verified.jsonl",
    "data/rejections.jsonl",
    "data/baseline_failures.jsonl",
    "data/run_metrics.jsonl",
    "data/provenance.jsonl",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()


def commit_record(validation_commits: list[str], release_commit: str) -> dict:
    """Which commit validated the data, which built the release, and whether the data moved.

    The validated data files must be byte-identical to the commit that last wrote them. The code
    changed since validation is listed, along with any change to the validation code itself, so a
    reader can see that code changed after validation while the validated data did not.
    """
    data_commit = _git("log", "-1", "--format=%H", "--", *VALIDATED_DATA)
    files = {}
    unchanged = True
    for path in VALIDATED_DATA:
        current = sha256_file(ROOT / path)
        blob = subprocess.run(["git", "show", f"{data_commit}:{path}"], cwd=ROOT,
                              capture_output=True, check=True).stdout  # fmt: skip
        unchanged &= hashlib.sha256(blob).hexdigest() == current
        files[path] = current
    changed = {
        commit: _git("diff", "--name-only", f"{commit}..{release_commit}", "--", "src").splitlines()
        for commit in validation_commits
    }
    touched_validation = {
        commit: _git("diff", "--name-only", f"{commit}..{release_commit}", "--",
                     *VALIDATION_CODE).splitlines()
        for commit in validation_commits
    }  # fmt: skip
    return {
        "validation": validation_commits,
        "release": release_commit,
        "validated_data_commit": data_commit,
        "validated_data_unchanged_since_that_commit": unchanged,
        "validated_data_sha256": files,
        "code_changed_since_validation": changed,
        "validation_code_changed_since_validation": touched_validation,
    }


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()


def assemble() -> tuple[list[ReleaseTask], tuple, dict[str, str], dict]:
    tasks = read_jsonl(VERIFIED_PATH, VerifiedTask)
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    provenance = {p.task_id: p for p in read_jsonl(PROVENANCE_PATH, Provenance)}
    missing = [t.task_id for t in tasks if t.task_id not in provenance]
    if missing:
        raise ReleaseError(f"{len(missing)} verified tasks lack provenance, e.g. {missing[:3]}")
    image_ids = {provenance[t.task_id].docker_image: provenance[t.task_id].image_id for t in tasks}

    human_type, spec_human = human_labels(TASK_TYPE_SHEET, SPEC_SHEET)
    spec_human = _adjudicated(spec_human)
    runs = latest_runs(read_jsonl(TASK_TYPE_RUNS_PATH, TaskTypeLabel))
    consensus = {t: decide(t, list(r.values())) for t, r in runs.items()}
    classifier_models = sorted(
        {r.model for c in consensus.values() if c.outcome == "applied" for r in c.runs if r.model}
    )

    merged = {t.task_id: candidates[t.task_id].merged_at for t in tasks}
    primary = assign_splits_per_repo(merged, {t.task_id: t.repo for t in tasks})
    global_split = assign_splits(merged)
    released = []
    for task in tasks:
        c = candidates[task.task_id]
        label = final_label(
            github_type=task.task_type,
            heuristic_spec_clear=task.spec_clear,
            human_type=human_type.get(task.task_id),
            human_spec=spec_human.get(task.task_id),
            consensus=consensus.get(task.task_id),
        )
        upstream = license_at(get_repo(task.repo).bare_path, task.base_commit)
        base = f"https://github.com/{c.repo}"
        fields = task.model_dump(exclude={"task_type", "spec_clear"})
        released.append(
            ReleaseTask(
                **fields,
                task_type=label.task_type,
                task_type_source=label.task_type_source,
                spec_clear=label.spec_clear,
                spec_clear_source=label.spec_clear_source,
                spec_check_clear=task.spec_clear,
                split=primary.splits[task.task_id],
                split_global=global_split.splits[task.task_id],
                merged_at=c.merged_at,
                created_at=c.created_at,
                pr_url=f"{base}/pull/{c.pr_number}",
                issue_url=f"{base}/issues/{c.issue_number}",
                upstream_license=upstream.spdx,
                upstream_license_file=upstream.file,
                upstream_license_sha256=upstream.file_sha256,
            )
        )
    unknown = [t.task_id for t in tasks if t.task_type == "unknown"]
    extra = {
        "commits": commit_record(
            sorted({provenance[t.task_id].repogym_commit for t in tasks}), _git_head()
        ),
        "classifier": {
            "models": classifier_models,
            "prompt_version": PROMPT_VERSION,
            "temperature": 0,
            "rule": f"{RUNS} independent runs; label applied only if all agree and every "
            f"confidence >= {MIN_CONFIDENCE}",
            "tasks_without_github_label": len(unknown),
            "outcomes": _outcomes([consensus.get(t) for t in unknown]),
        },
        "precedence": {
            "task_type": "human > github > classifier > unknown",
            "spec_clear": "human (adjudicated) > heuristic",
        },
    }
    return released, (primary, global_split), image_ids, extra


def _outcomes(results: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = "not run" if result is None else result.outcome
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def build_release(version: str, dry_run: bool = False) -> tuple[dict, list[str]]:
    """Write data/release/<version>/, or with ``dry_run`` build and validate it in a temp dir."""
    if not dry_run and (dirty := uncommitted_inputs()):
        raise ReleaseError(f"uncommitted release inputs; commit first:\n{dirty}")
    tasks, (primary, global_split), image_ids, extra = assemble()
    if not extra["commits"]["validated_data_unchanged_since_that_commit"]:
        raise ReleaseError("validated data files differ from their last commit; commit first")
    head = _git_head()
    if dry_run:
        temp = Path(tempfile.mkdtemp(prefix="repogym-release-"))
        try:
            out = temp / version
            manifest = write_release(
                out, version, tasks, primary, global_split, image_ids, head, extra
            )
            return manifest, validate_release(out)
        finally:
            shutil.rmtree(temp)
    out = RELEASE_DIR / version
    manifest = write_release(out, version, tasks, primary, global_split, image_ids, head, extra)
    problems = validate_release(out)
    if problems:
        raise ReleaseError(f"release written but failed validation: {problems[:5]}")
    return manifest, problems
