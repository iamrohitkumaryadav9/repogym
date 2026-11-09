"""Run episodes: reset -> agent -> collect -> grade -> flags -> ATIF trajectory + episode record.

Each run writes ``data/runs/<run_id>/episodes.jsonl`` (one provenance-rich record per episode)
and ``data/runs/<run_id>/trajectories/<task_id>.json`` (ATIF, validated before it's written).
"""

from __future__ import annotations

import functools
import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from repogym import accounting, trajectory
from repogym.config import CANDIDATES_PATH, DATA_DIR, RELEASE_DIR, ROOT
from repogym.env.budgets import Budgets
from repogym.env.docker import image_id
from repogym.env.environment import Environment, Observation, instructions, issue_text, now
from repogym.env.history import distinctive_lines
from repogym.env.sandbox import EnvSetupError
from repogym.flags import Evidence, compute, future_gold_lines
from repogym.grade import Grade, grade
from repogym.release import ReleaseTask
from repogym.validate import runnable_test_files

RUNS_DIR = DATA_DIR / "runs"


class Agent(Protocol):
    name: str
    version: str
    model_name: str | None

    def run(self, env: Environment, first: Observation) -> None: ...


class EpisodeRecord(BaseModel):
    run_id: str
    episode_id: str
    task_id: str
    agent: str
    agent_version: str
    model_name: str | None = None
    status: str  # graded | setup_error | grade_error | agent_error
    reward: int | None = None
    grade: Grade | None = None
    flags: dict[str, list] = Field(default_factory=dict)
    flags_raised: list[str] = Field(default_factory=list)
    done_reason: str | None = None
    steps: int = 0
    setup_seconds: float | None = None
    setup_checks: dict[str, bool] = Field(default_factory=dict)
    error: str = ""
    started_at: str
    ended_at: str | None = None
    # Provenance
    release: str
    tasks_sha256: str
    image: str
    image_digest: str
    repogym_commit: str
    repogym_dirty: bool
    budgets: Budgets
    seed: int | None = None
    tokens: dict[str, int | None] = Field(default_factory=dict)
    cost_usd: float | None = None
    trajectory: str | None = None
    # Past-history overlap of the fix (D6): recorded, never failed on; results break down by it.
    history_overlap: dict | None = None
    history_recoverable: bool | None = None
    past_gold_lines_observed: list[str] = Field(default_factory=list)  # informational (D6)
    # LLM provider account: declared by the operator (e.g. "free"), plus any quota ids the
    # provider named in 429 responses (e.g. "...-FreeTier"), which identify the tier.
    api_tier: str | None = None
    quota_ids_seen: list[str] = Field(default_factory=list)
    disk_written_mb: float | None = None  # writable layer at the end (setup included)
    peak_disk_mb: float | None = None
    # Week 3: cell identity and provenance, a coarse outcome category, and accounting.
    episode_key: str | None = None  # the experiment cell; with `attempt`, names the files
    attempt: int | None = None  # 1, 2, ... per cell; every attempt keeps its own files
    experiment: dict | None = None  # cell, experiment/config hashes, model and strategy settings
    error_type: str | None = None
    termination: str | None = None  # submitted | step_cap | wallclock_cap | context_failure
    #                                 | api_failure | harness_error
    accounting: dict = Field(default_factory=dict)


API_ERRORS = {"DailyQuotaExhausted", "ProviderUnavailable", "RequestRejected", "ModelUnavailable"}


def termination_of(record: EpisodeRecord) -> str:
    """The pre-registered termination category of an episode."""
    if record.status in ("setup_error", "grade_error"):
        return "harness_error"
    if record.error_type == "ContextOverflow":
        return "context_failure"
    if record.error_type in API_ERRORS:
        return "api_failure"
    if record.error_type:
        return "harness_error"
    return {
        "submitted": "submitted",
        "budget:steps": "step_cap",
        "budget:wall_clock": "wallclock_cap",
        # The elapsed safety cap only fires when throttling or an outage stalls an episode
        # (active time has its own cap), so it is an API failure, retried after reset (D17).
        "budget:elapsed": "api_failure",
    }.get(record.done_reason or "", "harness_error")


@functools.cache
def repogym_commit() -> tuple[str, bool]:
    """The code a run executes: HEAD and dirtiness, captured once when the process starts
    (the first call), so a later edit or commit can't be misattributed to running episodes."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True).stdout

    dirty = bool(git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock").strip())
    return git("rev-parse", "HEAD").strip(), dirty


def test_files(task_id: str) -> list[str]:
    """The test files grading runs: validation's exact selection (runnable files only), so
    rewards are comparable with v0.1 validation. Passing every touched test path would, e.g.,
    hand pytest a .yml data file and abort the run with a usage error."""
    with CANDIDATES_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            if f'"task_id":"{task_id}"' in line:
                return runnable_test_files(json.loads(line)["test_files"])
    raise KeyError(task_id)


_write_lock = threading.Lock()


def run_episode(
    task: ReleaseTask,
    agent: Agent,
    run_dir: Path,
    budgets: Budgets,
    release: str = "v0.1",
    seed: int | None = None,
    is_oracle: bool = False,
    grader: Callable = grade,
    api_tier: str | None = None,
    episode_key: str | None = None,
    experiment: dict | None = None,
    attempt: int | None = None,
) -> EpisodeRecord:
    manifest = json.loads((RELEASE_DIR / release / "manifest.json").read_text())
    commit, dirty = repogym_commit()
    # An agent may describe its own view of observations (the Week 3 tool agent clips and
    # masks them); the environment's first message then doesn't claim otherwise (D14).
    env = Environment(
        task, budgets, instruction_style=getattr(agent, "instruction_style", "default")
    )
    record = EpisodeRecord(
        run_id=run_dir.name,
        episode_id=env.episode_id,
        task_id=task.task_id,
        agent=agent.name,
        agent_version=agent.version,
        model_name=agent.model_name,
        status="setup_error",
        started_at=now(),
        release=release,
        tasks_sha256=manifest["tasks_sha256"],
        image=env.spec.image,
        image_digest=image_id(env.spec.image),
        repogym_commit=commit,
        repogym_dirty=dirty,
        budgets=budgets,
        seed=seed,
        api_tier=api_tier,
        episode_key=episode_key or task.task_id,
        attempt=attempt,
        experiment=experiment,
    )
    if record.image_digest != manifest["docker_images"].get(env.spec.image):
        record.error = "image digest differs from the release manifest"
        return _finish(record, run_dir)
    try:
        first = env.reset()
    except EnvSetupError as exc:
        record.error = str(exc)[:4000]
        env.close()
        return _finish(record, run_dir)
    assert env.setup_report is not None
    record.setup_seconds = env.setup_report.seconds
    record.setup_checks = {c.name: c.ok for c in env.setup_report.checks}
    record.history_overlap = env.probe_info.get("history_overlap")
    record.history_recoverable = (record.history_overlap or {}).get("recoverable")
    work = None
    try:
        try:
            agent.run(env, first)
        except Exception as exc:  # noqa: BLE001 - an agent crash is recorded, then graded as-is
            record.error = f"agent error: {type(exc).__name__}: {exc}"[:2000]
            record.error_type = type(exc).__name__
        state = env.state()
        record.done_reason = state.done_reason or ("agent_error" if record.error else None)
        record.steps = state.step_count
        record.quota_ids_seen = sorted(getattr(agent, "quota_ids_seen", None) or [])
        record.disk_written_mb, record.peak_disk_mb = state.disk_mb, state.peak_disk_mb
        try:
            work = env.collect()
        except Exception as exc:  # noqa: BLE001 - recorded; never turned into a reward
            record.status, record.error = "grade_error", f"collect failed: {exc}"[:2000]
    finally:
        env.close()
    if work is None:
        return _finish(record, run_dir)
    result = grader(task, work, test_files(task.task_id), env.read_agent_file)
    record.grade = result
    record.status = "graded" if result.status == "graded" else "grade_error"
    if record.error and record.status == "graded":
        record.status = "agent_error"
    record.reward = result.reward if result.status == "graded" else None
    gold = distinctive_lines(env.spec.bare_path, task.base_commit, task.gold_patch)
    past = set(env.probe_info.get("past_gold_lines", []))
    # The leak detector looks for FUTURE information only: gold lines that appear nowhere in
    # the reachable history. Past-history lines are context (D6) and are counted separately.
    future_gold = future_gold_lines(gold, past)
    flags = compute(env.steps, work, result, record.done_reason, future_gold, is_oracle=is_oracle)
    record.past_gold_lines_observed = sorted(
        {line for line in past for step in env.steps if line in step.observation}
    )
    truncated = [s.step for s in env.steps if s.truncated]
    if truncated:  # D14: a stored observation is no longer the full one; always visible
        flags.stored_observation_truncated.append(Evidence(detail=f"steps {truncated[:20]}"))
    record.flags = {k: [e.model_dump() for e in v] for k, v in flags}
    record.flags_raised = flags.raised()
    record.tokens, record.cost_usd = _totals(env)
    if any(s.metrics for s in env.steps) or env.events:
        record.accounting = accounting.summarize(env.steps, env.events, agent.model_name)
        record.tokens = {
            "prompt": record.accounting["input_tokens"],
            "completion": record.accounting["output_tokens"] + record.accounting["thinking_tokens"],
        }
        record.cost_usd = record.accounting["cost_usd"]
        state = env.state()  # D17: active time is capped, waiting is only reported
        record.accounting["episode_seconds"] = {
            "elapsed": state.elapsed_seconds,
            "active": state.active_seconds,
            "waiting": state.waiting_seconds,
        }
    record.termination = termination_of(record)
    record.ended_at = now()
    doc = trajectory.build(
        episode_id=env.episode_id,
        session_id=run_dir.name,
        agent_name=agent.name,
        agent_version=agent.version,
        model_name=agent.model_name,
        system_text=instructions(task, budgets),
        user_text=issue_text(task),
        started_at=env.state().started_at or record.started_at,
        steps=env.steps,
        events=env.events,
        final_extra={
            "repogym": {
                "reward": record.reward,
                "status": record.status,
                "grade": result.model_dump(),
                "flags": record.flags,
                "flags_raised": record.flags_raised,
                "done_reason": record.done_reason,
                "termination": record.termination,
                "accounting": record.accounting,
            }
        },
        root_extra={
            "repogym": {
                "task_id": task.task_id,
                "release": release,
                "tasks_sha256": record.tasks_sha256,
                "episode_id": env.episode_id,
                "image_digest": record.image_digest,
                "repogym_commit": commit,
                "budgets": budgets.model_dump(),
                "api_tier": api_tier,
                "quota_ids_seen": record.quota_ids_seen,
                "seed": seed,
                "episode_key": record.episode_key,
                "experiment": experiment,
            }
        },
        token_totals={
            "total_prompt_tokens": record.tokens.get("prompt"),
            "total_completion_tokens": record.tokens.get("completion"),
            "total_cost_usd": record.cost_usd,
        },
    )
    errors = trajectory.validate(doc)
    if errors:
        raise ValueError(f"ATIF validation failed for {task.task_id}: {errors[:5]}")
    stem = unique_stem(run_dir, file_stem(record.episode_key or task.task_id, record.attempt))
    diff_path = run_dir / "diffs" / f"{stem}.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(work.diff, encoding="utf-8", errors="surrogateescape")
    path = run_dir / "trajectories" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    record.trajectory = str(path.relative_to(run_dir))
    return _finish(record, run_dir)


def file_stem(key: str, attempt: int | None) -> str:
    return key if attempt is None else f"{key}__a{attempt}"


def unique_stem(run_dir: Path, stem: str) -> str:
    """Never overwrite an earlier episode's trajectory or diff: suffix on any collision."""
    with _write_lock:
        candidate, n = stem, 1
        while (run_dir / "trajectories" / f"{candidate}.json").exists() or (
            run_dir / "diffs" / f"{candidate}.diff"
        ).exists():
            n += 1
            candidate = f"{stem}__dup{n}"
        (run_dir / "trajectories").mkdir(parents=True, exist_ok=True)
        (run_dir / "trajectories" / f"{candidate}.json").touch()  # reserve the name
        return candidate


def _totals(env: Environment) -> tuple[dict[str, int | None], float | None]:
    prompt = completion = 0
    cost: float | None = 0.0
    seen = False
    for step in env.steps:
        if step.metrics:
            seen = True
            prompt += step.metrics.get("prompt_tokens") or 0
            completion += step.metrics.get("completion_tokens") or 0
            c = step.metrics.get("cost_usd")
            cost = None if c is None or cost is None else cost + c
    if not seen:
        return {}, None
    return {"prompt": prompt, "completion": completion}, cost


def _finish(record: EpisodeRecord, run_dir: Path) -> EpisodeRecord:
    record.ended_at = record.ended_at or now()
    record.termination = record.termination or termination_of(record)
    run_dir.mkdir(parents=True, exist_ok=True)
    with _write_lock, (run_dir / "episodes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")
    return record


def run_many(
    tasks: list[ReleaseTask],
    make_agent: Callable[[ReleaseTask], Agent],
    run_dir: Path,
    budgets: Budgets,
    workers: int = 2,
    is_oracle: bool = False,
    on_done: Callable[[EpisodeRecord], None] | None = None,
    api_tier: str | None = None,
) -> list[EpisodeRecord]:
    done_ids = set()
    if (run_dir / "episodes.jsonl").exists():
        done_ids = {
            json.loads(line)["task_id"]
            for line in (run_dir / "episodes.jsonl").read_text().splitlines()
            if line
        }
    pending = [t for t in tasks if t.task_id not in done_ids]
    repogym_commit()  # pin provenance to the code this process started with

    def one(task: ReleaseTask) -> EpisodeRecord:
        record = run_episode(
            task, make_agent(task), run_dir, budgets, is_oracle=is_oracle, api_tier=api_tier
        )
        if on_done:
            on_done(record)
        return record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, pending))
