"""Compute FAIL_TO_PASS and PASS_TO_PASS, apply the validity checks, and record outcomes."""

from __future__ import annotations

import os
import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import structlog
from pydantic import BaseModel

from repogym.config import (
    BASELINE_PATH,
    CONTAINER_MEMORY_BYTES,
    OOM_ORACLE_PEAK_FRACTION,
    ORACLE_TEST_SECONDS_LIMIT,
    PROVENANCE_PATH,
    REJECTIONS_PATH,
    RUN_METRICS_PATH,
    VERIFIED_PATH,
    get_repo,
)
from repogym.env.docker import DockerUnavailable, run_tests
from repogym.env.results import test_belongs_to_module
from repogym.mining.patches import is_runnable_test_file
from repogym.models import (
    BaselineRecord,
    Candidate,
    Provenance,
    Rejection,
    RunMetrics,
    RunStage,
    TaskType,
    TestRun,
    TestStatus,
    VerifiedTask,
)
from repogym.spec import check_spec
from repogym.tasktype import TaskTypeClassifier, classify_task_type

log = structlog.get_logger()

ORACLE_RUNS = 3
_FAILING = {TestStatus.FAILED, TestStatus.ERROR}

Model = TypeVar("Model", bound=BaseModel)


@dataclass(frozen=True)
class Lists:
    fail_to_pass: list[str]
    pass_to_pass: list[str]


def baseline_excluded(baseline: TestRun, test_id: str) -> bool:
    """True if a test failed at base_commit, or its module could not even be imported there."""
    if baseline.tests.get(test_id) in _FAILING:
        return True
    return any(test_belongs_to_module(test_id, mod) for mod in baseline.collection_errors)


def compute_lists(baseline: TestRun, unsolved: TestRun, oracle: TestRun) -> Lists:
    """Derive the test lists. Tests failing at baseline can never enter either one.

    A test counts as failing before the fix if it failed, errored, or never ran because its
    module failed to import. Tests merely absent from the unsolved run are ignored.
    """
    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    for test_id, status in oracle.tests.items():
        if status is not TestStatus.PASSED or baseline_excluded(baseline, test_id):
            continue
        before = unsolved.tests.get(test_id)
        if before is TestStatus.PASSED:
            pass_to_pass.append(test_id)
        elif before in _FAILING or (
            before is None
            and any(test_belongs_to_module(test_id, mod) for mod in unsolved.collection_errors)
        ):
            fail_to_pass.append(test_id)
    return Lists(sorted(fail_to_pass), sorted(pass_to_pass))


def explain_no_fail_to_pass(baseline: TestRun, unsolved: TestRun, oracle: TestRun) -> Rejection:
    """Say why FAIL_TO_PASS came out empty. ``task_id`` and ``repo`` are filled by the caller."""
    failing = {t for t, s in unsolved.tests.items() if s in _FAILING}
    if not failing and not unsolved.collection_errors:
        return Rejection(
            task_id="",
            repo="",
            reason="check1_nothing_fails_without_fix",
            detail="Every touched test already passes before the fix",
        )
    fixed = [t for t in failing if oracle.tests.get(t) is TestStatus.PASSED]
    if fixed or (unsolved.collection_errors and not oracle.collection_errors and oracle.tests):
        return Rejection(
            task_id="",
            repo="",
            reason="all_fixed_tests_fail_at_baseline",
            detail=baseline.detail or f"collection errors: {baseline.collection_errors[:3]}",
        )
    return Rejection(
        task_id="",
        repo="",
        reason="check2_oracle_does_not_fix",
        detail=oracle.detail or f"collection errors: {oracle.collection_errors[:3]}",
    )


def is_solved(run: TestRun, lists: Lists) -> bool:
    return all(
        run.tests.get(t) is TestStatus.PASSED for t in lists.fail_to_pass + lists.pass_to_pass
    )


def inconsistent_tests(runs: list[TestRun]) -> list[str]:
    """Tests whose status differs across runs; a test missing from one run counts as differing."""
    all_ids = set().union(*(run.tests for run in runs))
    return sorted(t for t in all_ids if len({run.tests.get(t) for run in runs}) > 1)


def slow_listed_tests(runs: list[TestRun], lists: Lists, limit: float) -> list[str]:
    """Listed tests that came within reach of the per-test timeout in some oracle run."""
    listed = set(lists.fail_to_pass + lists.pass_to_pass)
    slow = {
        f"{test} ({run.test_seconds[test]:.1f}s)"
        for run in runs
        for test in listed
        if run.test_seconds.get(test, 0.0) > limit
    }
    return sorted(slow)


def annotate(
    candidate: Candidate, classifier: TaskTypeClassifier | None = None
) -> tuple[TaskType, bool, list[str]]:
    """task_type, spec_clear and spec_issues for a task. Needs the repo's bare clone."""
    task_type = classify_task_type(candidate, classifier)
    try:
        result = check_spec(
            get_repo(candidate.repo),
            candidate.base_commit,
            candidate.gold_patch,
            candidate.test_patch,
            candidate.issue_title,
            candidate.issue_body,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the task rather than aborting the run
        return task_type, False, [f"spec check could not run: {type(exc).__name__}: {exc}"]
    return task_type, result.spec_clear, result.spec_issues


def _stage_rejection(label: str, run: TestRun) -> str:
    """Map an incomplete run to a rejection reason."""
    return {
        RunStage.PATCH_FAILED: "gold_patch_apply_failed",
        RunStage.TEST_PATCH_FAILED: "test_patch_apply_failed",
        RunStage.NO_TEST_FILES: "no_runnable_test_files",
        RunStage.TIMEOUT: f"{label}_timeout",
        RunStage.OOM: f"{label}_out_of_memory",
    }.get(run.stage, f"env_{run.stage.value}")


@dataclass(frozen=True)
class Outcome:
    candidate: Candidate
    verified: VerifiedTask | None
    rejection: Rejection | None
    baseline: BaselineRecord | None
    metrics: RunMetrics | None
    pytest_version: str | None = None


Runner = Callable[..., TestRun]


Annotator = Callable[[Candidate], tuple[TaskType, bool, list[str]]]


def oom_counts_as_failure(run: TestRun) -> bool:
    """An OOM-killed run is usable only if we know which test was running when it died."""
    return run.stage is RunStage.OOM and run.killed_test is not None


def oracle_peak_problem(oracle_runs: list[TestRun], limit_bytes: int) -> str | None:
    """Why an OOM before the fix can't be trusted; None if the fixed code is comfortably light."""
    threshold = limit_bytes * OOM_ORACLE_PEAK_FRACTION
    peaks = [run.peak_memory_bytes for run in oracle_runs]
    if any(peak is None for peak in peaks):
        return "oracle peak memory unknown"
    worst = max(peak for peak in peaks if peak is not None)
    if worst >= threshold:
        return f"oracle peak {worst / 1024**2:.0f} MiB is not under {threshold / 1024**2:.0f} MiB"
    return None


def validate_candidate(
    candidate: Candidate,
    runner: Runner = run_tests,
    annotator: Annotator | None = None,
    memory_limit_bytes: int = CONTAINER_MEMORY_BYTES,
) -> Outcome:
    """Run the checks for one candidate, stopping at the first failure."""
    spec = get_repo(candidate.repo)
    test_files = [path for path in candidate.test_files if is_runnable_test_file(path)]
    runs: dict[str, TestRun] = {}
    record: BaselineRecord | None = None

    def metrics(oracle_runs: list[TestRun] | None = None) -> RunMetrics:
        seconds = None
        if oracle_runs:
            seconds = sum(r.duration_seconds for r in oracle_runs) / len(oracle_runs)
        peaks = {label: run.peak_memory_bytes for label, run in runs.items()}
        return RunMetrics(
            task_id=candidate.task_id, oracle_seconds=seconds, peak_memory_bytes=peaks
        )

    def reject(reason: str, detail: str = "") -> Outcome:
        rejection = Rejection(
            task_id=candidate.task_id, repo=candidate.repo, reason=reason, detail=detail[-1500:]
        )
        log.info("rejected", task=candidate.task_id, reason=reason)
        return Outcome(candidate, None, rejection, record, metrics(), _versions(runs))

    if not test_files:
        return reject("no_runnable_test_files", f"touched: {candidate.test_files[:5]}")

    def run(label: str, patch: str, test_patch: str, **kwargs) -> TestRun:
        result = runner(spec, candidate.base_commit, patch, test_patch, test_files, **kwargs)
        runs[label] = result
        return result

    baseline = run("baseline", "", "", require_test_files=False)
    if baseline.stage is not RunStage.COMPLETED:
        return reject(_stage_rejection("baseline", baseline), baseline.detail)
    record = BaselineRecord(
        task_id=candidate.task_id,
        repo=candidate.repo,
        failing=sorted(t for t, s in baseline.tests.items() if s in _FAILING),
        collection_errors=baseline.collection_errors,
    )

    oracle = run("oracle1", candidate.gold_patch, candidate.test_patch)
    if oracle.stage is not RunStage.COMPLETED:
        return reject(_stage_rejection("oracle", oracle), oracle.detail)

    # Check 1: the unsolved state must fail. Running out of memory there is a failure of the
    # test that was running, provided the oracle later proves light (checked below).
    unsolved = run("unsolved", "", candidate.test_patch)
    if unsolved.stage is not RunStage.COMPLETED and not oom_counts_as_failure(unsolved):
        return reject(_stage_rejection("unsolved", unsolved), unsolved.detail)
    lists = compute_lists(baseline, unsolved, oracle)
    if not lists.fail_to_pass:
        why = explain_no_fail_to_pass(baseline, unsolved, oracle)
        return reject(why.reason, why.detail)

    # Check 3: an empty patch must not count as solved.
    noop = run("noop", "", candidate.test_patch)
    if noop.stage is not RunStage.COMPLETED and not oom_counts_as_failure(noop):
        return reject(_stage_rejection("noop", noop), noop.detail)
    if is_solved(noop, lists):
        return reject("check3_noop_counts_as_solved")
    passing = [t for t in lists.fail_to_pass if noop.tests.get(t) is TestStatus.PASSED]
    if passing:
        return reject("flaky_unsolved_state", f"Passed in only one no-fix run: {passing[:5]}")

    # Check 2 and flakiness: the oracle must be stable and solve every listed test.
    oracle_runs = [oracle]
    for index in range(2, ORACLE_RUNS + 1):
        again = run(f"oracle{index}", candidate.gold_patch, candidate.test_patch)
        if again.stage is not RunStage.COMPLETED:
            return reject(_stage_rejection("oracle", again), again.detail)
        oracle_runs.append(again)
    flaky = inconsistent_tests(oracle_runs)
    if flaky:
        return reject("flaky_oracle", f"{len(flaky)} inconsistent: {flaky[:5]}")
    if not all(is_solved(item, lists) for item in oracle_runs):
        return reject("check2_oracle_does_not_fix", "Listed test failed in a repeat run")
    slow = slow_listed_tests(oracle_runs, lists, ORACLE_TEST_SECONDS_LIMIT)
    if slow:
        detail = f"Over {ORACLE_TEST_SECONDS_LIMIT:.0f}s with the fix: {slow[:5]}"
        return reject("oracle_too_close_to_timeout", detail)
    if unsolved.stage is RunStage.OOM or noop.stage is RunStage.OOM:
        problem = oracle_peak_problem(oracle_runs, memory_limit_bytes)
        if problem:
            return reject("unsolved_out_of_memory", problem)

    task_type, spec_clear, spec_issues = (annotator or annotate)(candidate)

    verified = VerifiedTask(
        task_id=candidate.task_id,
        repo=candidate.repo,
        base_commit=candidate.base_commit,
        issue_title=candidate.issue_title,
        issue_body=candidate.issue_body,
        gold_patch=candidate.gold_patch,
        test_patch=candidate.test_patch,
        fail_to_pass=lists.fail_to_pass,
        pass_to_pass=lists.pass_to_pass,
        docker_image=spec.image,
        task_type=task_type,
        spec_clear=spec_clear,
        spec_issues=spec_issues,
    )
    log.info(
        "verified",
        task=candidate.task_id,
        fail_to_pass=len(lists.fail_to_pass),
        pass_to_pass=len(lists.pass_to_pass),
    )
    return Outcome(candidate, verified, None, record, metrics(oracle_runs), _versions(runs))


def _versions(runs: dict[str, TestRun]) -> str | None:
    return next((run.pytest_version for run in runs.values() if run.pytest_version), None)


@dataclass(frozen=True)
class RunContext:
    """Facts shared by every task in one validation run, recorded in each task's provenance."""

    image_ids: dict[str, str]
    repogym_commit: str
    repogym_dirty: bool

    @classmethod
    def current(cls) -> RunContext:
        from repogym.config import REPOS, ROOT
        from repogym.env.docker import image_id

        def git(*args: str) -> str:
            done = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
            return done.stdout.strip()

        return cls(
            image_ids={spec.image: image_id(spec.image) for spec in REPOS.values()},
            repogym_commit=git("rev-parse", "HEAD"),
            # Only code counts: the data files under data/ change as validation runs.
            repogym_dirty=bool(
                git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock")
            ),
        )

    def provenance(self, outcome: Outcome) -> Provenance:
        image = get_repo(outcome.candidate.repo).image
        return Provenance(
            task_id=outcome.candidate.task_id,
            status="verified" if outcome.verified is not None else "rejected",
            reason=outcome.rejection.reason if outcome.rejection else "",
            docker_image=image,
            image_id=self.image_ids.get(image, ""),
            repogym_commit=self.repogym_commit,
            repogym_dirty=self.repogym_dirty,
            pytest_version=outcome.pytest_version,
            validated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


class JsonlStore:
    """Append-only JSONL that survives crashes: every record is flushed and fsynced."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # A crash mid-write can leave a partial last line; end it so the next append starts clean.
        if path.exists() and path.stat().st_size:
            with path.open("rb+") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")

    def append(self, record: BaseModel) -> None:
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_jsonl(path: Path, model: type[Model]) -> list[Model]:
    """Read records, skipping any partial line a crash left behind."""
    if not path.exists():
        return []
    records: list[Model] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValueError:
            log.warning("skipping_corrupt_line", path=str(path))
    return records


def forget_rejections(path: Path, reasons: set[str]) -> int:
    """Remove rejections with these reasons so they get re-validated. Returns how many."""
    return rewrite_jsonl(path, Rejection, lambda r: r.reason not in reasons)


def rewrite_jsonl(path: Path, model: type[Model], keep: Callable[[Model], bool]) -> int:
    """Atomically rewrite a JSONL file keeping matching records; returns how many were dropped."""
    records = read_jsonl(path, model)
    kept = [r for r in records if keep(r)]
    if path.exists():
        temp = path.with_suffix(".tmp")
        temp.write_text("".join(r.model_dump_json() + "\n" for r in kept), encoding="utf-8")
        temp.replace(path)
    return len(records) - len(kept)


def forget_tasks(task_ids: set[str]) -> int:
    """Remove every record of these tasks so the next run validates them from scratch."""
    removed = rewrite_jsonl(VERIFIED_PATH, VerifiedTask, lambda r: r.task_id not in task_ids)
    removed += rewrite_jsonl(REJECTIONS_PATH, Rejection, lambda r: r.task_id not in task_ids)
    rewrite_jsonl(BASELINE_PATH, BaselineRecord, lambda r: r.task_id not in task_ids)
    rewrite_jsonl(RUN_METRICS_PATH, RunMetrics, lambda r: r.task_id not in task_ids)
    rewrite_jsonl(PROVENANCE_PATH, Provenance, lambda r: r.task_id not in task_ids)
    return removed


def already_validated() -> set[str]:
    done = {t.task_id for t in read_jsonl(VERIFIED_PATH, VerifiedTask)}
    return done | {r.task_id for r in read_jsonl(REJECTIONS_PATH, Rejection)}


def validate_all(
    candidates: list[Candidate],
    workers: int,
    runner: Runner = run_tests,
    annotator: Annotator | None = None,
    context: RunContext | None = None,
) -> Counter[str]:
    """Validate candidates in parallel, recording each as it finishes.

    Reruns skip anything already recorded. If Docker itself goes away (say the laptop slept), the
    in-flight tasks are left unrecorded so a rerun retries them, rather than rejecting them for a
    fault that was not theirs.
    """
    verified_store = JsonlStore(VERIFIED_PATH)
    rejection_store = JsonlStore(REJECTIONS_PATH)
    baseline_store = JsonlStore(BASELINE_PATH)
    metrics_store = JsonlStore(RUN_METRICS_PATH)
    provenance_store = JsonlStore(PROVENANCE_PATH)
    context = context or RunContext.current()
    if context.repogym_dirty:
        log.warning("uncommitted_changes", commit=context.repogym_commit)

    done = already_validated()
    pending = [c for c in candidates if c.task_id not in done]
    log.info("validation_start", total=len(candidates), skipped=len(done), pending=len(pending))
    tally: Counter[str] = Counter()

    def record(outcome: Outcome) -> None:
        # Provenance goes first: a task with results but no provenance would be unauditable.
        provenance_store.append(context.provenance(outcome))
        if outcome.baseline is not None:
            baseline_store.append(outcome.baseline)
        if outcome.metrics is not None:
            metrics_store.append(outcome.metrics)
        if outcome.verified is not None:
            verified_store.append(outcome.verified)
            tally["verified"] += 1
        elif outcome.rejection is not None:
            rejection_store.append(outcome.rejection)
            tally[outcome.rejection.reason] += 1

    queue = iter(pending)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = set()
        failure: DockerUnavailable | None = None
        while True:
            while failure is None and len(active) < workers:
                candidate = next(queue, None)
                if candidate is None:
                    break
                active.add(pool.submit(validate_candidate, candidate, runner, annotator))
            if not active:
                break
            finished, active = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                try:
                    record(future.result())
                except DockerUnavailable as exc:
                    failure = exc
            completed = sum(tally.values())
            if completed and completed % 10 == 0:
                log.info("validation_progress", completed=completed, of=len(pending))
    if failure is not None:
        raise failure
    return tally
