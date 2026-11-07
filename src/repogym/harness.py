"""Run episodes: reset -> agent -> collect -> grade -> flags -> ATIF trajectory + episode record.

Each run writes ``data/runs/<run_id>/episodes.jsonl`` (one provenance-rich record per episode)
and ``data/runs/<run_id>/trajectories/<task_id>.json`` (ATIF, validated before it's written).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from repogym import trajectory
from repogym.config import CANDIDATES_PATH, DATA_DIR, RELEASE_DIR, ROOT
from repogym.env.budgets import Budgets
from repogym.env.docker import image_id
from repogym.env.environment import Environment, Observation, instructions, issue_text, now
from repogym.env.history import distinctive_lines
from repogym.env.sandbox import EnvSetupError
from repogym.flags import compute
from repogym.grade import Grade, grade
from repogym.release import ReleaseTask

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
    disk_written_mb: float | None = None  # writable layer at the end (setup included)
    peak_disk_mb: float | None = None


def repogym_commit() -> tuple[str, bool]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True).stdout

    dirty = bool(git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock").strip())
    return git("rev-parse", "HEAD").strip(), dirty


def test_files(task_id: str) -> list[str]:
    with CANDIDATES_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            if f'"task_id":"{task_id}"' in line:
                return json.loads(line)["test_files"]
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
) -> EpisodeRecord:
    manifest = json.loads((RELEASE_DIR / release / "manifest.json").read_text())
    commit, dirty = repogym_commit()
    env = Environment(task, budgets)
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
        state = env.state()
        record.done_reason = state.done_reason or ("agent_error" if record.error else None)
        record.steps = state.step_count
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
    flags = compute(env.steps, work, result, record.done_reason, gold, is_oracle=is_oracle)
    record.flags = {k: [e.model_dump() for e in v] for k, v in flags}
    record.flags_raised = flags.raised()
    record.tokens, record.cost_usd = _totals(env)
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
        final_extra={
            "repogym": {
                "reward": record.reward,
                "status": record.status,
                "grade": result.model_dump(),
                "flags": record.flags,
                "flags_raised": record.flags_raised,
                "done_reason": record.done_reason,
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
                "seed": seed,
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
    diff_path = run_dir / "diffs" / f"{task.task_id}.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(work.diff, encoding="utf-8", errors="surrogateescape")
    path = run_dir / "trajectories" / f"{task.task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    record.trajectory = str(path.relative_to(run_dir))
    return _finish(record, run_dir)


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
) -> list[EpisodeRecord]:
    done_ids = set()
    if (run_dir / "episodes.jsonl").exists():
        done_ids = {
            json.loads(line)["task_id"]
            for line in (run_dir / "episodes.jsonl").read_text().splitlines()
            if line
        }
    pending = [t for t in tasks if t.task_id not in done_ids]

    def one(task: ReleaseTask) -> EpisodeRecord:
        record = run_episode(task, make_agent(task), run_dir, budgets, is_oracle=is_oracle)
        if on_done:
            on_done(record)
        return record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, pending))
