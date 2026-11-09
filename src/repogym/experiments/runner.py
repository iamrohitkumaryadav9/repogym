"""Run an experiment: one worker per model, in parallel, resumable and idempotent.

* Each model has its own provider and rate limiter. All models share one quota ledger on disk
  (D11), so throughput is governed per model and a model can't exceed its daily quota across
  restarts.
* A cell (model, strategy, task, repeat) is done once its latest episode record exists and isn't
  an ``api_failure``. Episodes interrupted by a crash, sleep or reboot leave no record and simply
  rerun.
* Pre-registered exclusion rules:
  * a ``harness_error`` episode is rerun exactly once, and both records are kept and reported;
  * an ``api_failure`` caused by daily quota exhaustion, or by the elapsed safety cap (an
    episode stalled by throttling, D17), is retried after that model's reset, while the
    worker sleeps and the other models keep running. A cell that hits the elapsed cap twice
    is given up on and reported.
* The guard (test-split protection, pilot hygiene) runs before anything starts.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import structlog
from dotenv import load_dotenv

from repogym.agents.tool_agent import ToolAgent
from repogym.config import DATA_DIR, ROOT
from repogym.context.strategies import ContextManager
from repogym.context.tokens import TokenEstimator
from repogym.experiments import guard
from repogym.experiments.spec import Cell, Experiment, release_tasks
from repogym.harness import EpisodeRecord, _write_lock, run_episode
from repogym.providers.gemini import GeminiProvider, GeminiSettings
from repogym.ratelimit import AttemptLog, LimitsConfig, QuotaLedger, RateLimiter

log = structlog.get_logger()
LEDGER_PATH = DATA_DIR / "quota" / "ledger.json"
ATTEMPTS_DIR = DATA_DIR / "quota"  # attempts-<UTC day>.jsonl: every API attempt (D16)
HARNESS_ERROR_RERUNS = 1
# A cell gets at most this many api_failure attempts; then it is recorded as api_failure,
# reported, and not retried (D16). Daily-quota interruptions don't count: they come from our
# own quota scheduling, not the model, and are retried after the reset.
MAX_API_FAILURES = 3


class Heartbeat:
    """In-progress state of one model's worker, for `repogym status` (one small JSON file)."""

    def __init__(self, run_dir: Path, model: str, clock=time.time) -> None:
        self.path = run_dir / "heartbeat" / f"{model}.json"
        self.model, self.clock = model, clock
        self.state: dict = {"model": model, "state": "starting"}

    def _write(self, **changes) -> None:
        self.state.update(changes, updated_at=self.clock())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.path)

    def start(self, cell: str, attempt: int) -> None:
        self.state = {"model": self.model}
        self._write(
            state="running",
            cell=cell,
            attempt=attempt,
            episode_started_at=self.clock(),
            step=0,
            last_model_call_at=None,
        )

    def container(self, name: str) -> None:
        self._write(container=name)

    def progress(self, step: int) -> None:
        self._write(step=step, last_model_call_at=self.clock())

    def idle(self, termination: str | None) -> None:
        self._write(state="between_episodes", last_termination=termination)

    def sleeping(self, until: float, reason: str | None) -> None:
        self._write(state="sleeping", sleeping_until=until, reason=reason)

    def finished(self) -> None:
        self._write(state="done")


def write_pid_file(run_dir: Path, models: list[str]) -> Path:
    """Record this runner's PID and process group in the run directory, so it can be stopped by
    exactly that process group (never by a name pattern; RUNBOOK)."""
    path = run_dir / "pids" / f"runner-{'+'.join(models)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "pgid": os.getpgid(0),
                "models": models,
                "argv": sys.argv,
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    return path


def gemini_api_key() -> str:
    """GEMINI_API_KEY from the environment or .env; the only setting Week 3 runs need."""
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (in the environment or .env)")
    return key


def latest_records(run_dir: Path) -> dict[str, list[dict]]:
    """Episode records per cell, in order. Safe while records are being appended: in this
    process it takes the writer's lock; across processes, a final line without its newline is
    a write in progress and is skipped (any other malformed line still raises)."""
    path = run_dir / "episodes.jsonl"
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with _write_lock:
        text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    complete, partial = lines[:-1], lines[-1]  # text ends with "\n" unless a write is under way
    for line in complete:
        if line.strip():
            record = json.loads(line)
            out.setdefault(record.get("episode_key") or record["task_id"], []).append(record)
    if partial.strip():
        try:
            record = json.loads(partial)
        except ValueError:
            return out  # another process is still writing this record
        out.setdefault(record.get("episode_key") or record["task_id"], []).append(record)
    return out


def counted_api_failures(records: list[dict]) -> int:
    return sum(
        r.get("termination") == "api_failure" and r.get("error_type") != "DailyQuotaExhausted"
        for r in records
    )


def cell_done(records: list[dict]) -> bool:
    """Done unless never run, the latest was a retryable api_failure, or it needs its rerun."""
    if not records:
        return False
    latest = records[-1].get("termination")
    if latest == "api_failure":
        return counted_api_failures(records) >= MAX_API_FAILURES
    if latest == "harness_error":
        reruns = sum(r.get("termination") == "harness_error" for r in records)
        return reruns > HARNESS_ERROR_RERUNS
    return True


def attempt_summary(record: dict, index: int) -> dict:
    return {
        "attempt": record.get("attempt") or index,
        "episode_id": record.get("episode_id"),
        "termination": record.get("termination"),
        "error_type": record.get("error_type"),
        "steps": record.get("steps"),
        "trajectory": record.get("trajectory"),
    }


def provenance(experiment: Experiment, cell: Cell, previous: list[dict] | None = None) -> dict:
    previous = previous or []
    return {
        "attempt": len(previous) + 1,
        "previous_attempts": [attempt_summary(r, i) for i, r in enumerate(previous, 1)],
        "name": experiment.name,
        "kind": experiment.kind,
        "pilot": experiment.kind == "pilot",
        "cell": {
            "model": cell.model,
            "strategy": cell.strategy,
            "task_id": cell.task_id,
            "repeat": cell.repeat,
        },
        "experiment_file": str(experiment.path.name) if experiment.path else None,
        "experiment_sha256": experiment.file_sha256,
        "resolved_config_sha256": experiment.resolved_sha256,
        "frozen_config": experiment.frozen_config,
        "model_settings": experiment.model_settings[cell.model],
        "context": experiment.context.as_dict(),
        "limits_margin": experiment.resolved()["limits_margin"],
        "retry": experiment.retry.as_dict(),
    }


def make_provider(model: str, settings: dict, api_key: str, limiter, policy=None, attempt_log=None):
    if settings.get("provider", "gemini") != "gemini":
        raise ValueError(f"provider {settings.get('provider')!r} isn't wired into the runner yet")
    return GeminiProvider(
        GeminiSettings(
            model=model,
            sampling=settings.get("sampling") or {},
            thinking_level=settings.get("thinking_level"),
            max_output_tokens=settings.get("max_output_tokens", 8192),
        ),
        api_key=api_key,
        limiter=limiter,
        policy=policy,
        attempt_log=attempt_log,
    )


def make_agent(experiment: Experiment, cell: Cell, provider) -> ToolAgent:
    # countTokens is never called in timed runs (D20): the calibrated estimator, corrected
    # for re-billed thinking, sizes every prompt.
    cm = ContextManager(cell.strategy, experiment.context, TokenEstimator())
    return ToolAgent(provider, cm, settings=experiment.model_settings[cell.model])


class Runner:
    def __init__(
        self,
        experiment: Experiment,
        api_key: str,
        final_test_run: bool = False,
        episode_fn: Callable = run_episode,
        provider_fn: Callable = make_provider,
        ledger: QuotaLedger | None = None,
        clock=time.time,
        sleep=time.sleep,
        on_done: Callable[[EpisodeRecord], None] | None = None,
        guard_fn: Callable = guard.check,
        attempt_log: AttemptLog | None = None,
    ) -> None:
        guard_fn(experiment, final_test_run)
        self.experiment, self.api_key = experiment, api_key
        self.limits = LimitsConfig.load(experiment.limits_path)
        self.ledger = ledger or QuotaLedger(LEDGER_PATH, self.limits.reset_timezone)
        self.episode_fn, self.provider_fn = episode_fn, provider_fn
        self.clock, self.sleep, self.on_done = clock, sleep, on_done
        self.tasks = release_tasks(experiment.release)
        self.stop = threading.Event()
        self.attempt_log = attempt_log if attempt_log is not None else AttemptLog(ATTEMPTS_DIR)

    def pending(self, model: str) -> list[Cell]:
        done = latest_records(self.experiment.run_dir)
        return [
            c
            for c in self.experiment.cells()
            if c.model == model and not cell_done(done.get(c.key, []))
        ]

    def worker(self, model: str) -> None:
        limiter = RateLimiter(model, self.limits, self.ledger, clock=self.clock, sleep=self.sleep)
        provider = self.provider_fn(
            model,
            self.experiment.model_settings[model],
            self.api_key,
            limiter,
            self.experiment.retry,
            self.attempt_log,
        )
        beat = Heartbeat(self.experiment.run_dir, model, self.clock)
        while not self.stop.is_set():
            cells = self.pending(model)
            if not cells:
                beat.finished()
                return
            cell = cells[0]
            previous = latest_records(self.experiment.run_dir).get(cell.key, [])
            attempt = len(previous) + 1
            beat.start(cell.key, attempt)
            agent = make_agent(self.experiment, cell, provider)
            agent.progress = beat.progress
            record = self.episode_fn(
                self.tasks[cell.task_id],
                agent,
                self.experiment.run_dir,
                self.experiment.budgets,
                release=self.experiment.release,
                api_tier="free",
                episode_key=cell.key,
                experiment=provenance(self.experiment, cell, previous),
                attempt=attempt,
                on_env=lambda env: beat.container(env.sandbox.name),
            )
            beat.idle(record.termination)
            if self.on_done:
                self.on_done(record)
            # Daily quota, or an episode stalled past the elapsed safety cap by throttling:
            # wait for this model's reset, then retry (pre-registered exclusion rules, D16/D17).
            retry = not cell_done(latest_records(self.experiment.run_dir).get(cell.key, []))
            if not retry:
                continue
            if record.error_type == "DailyQuotaExhausted" or record.done_reason == "budget:elapsed":
                reset = self.ledger.next_reset(self.clock())
                wait = max(reset - self.clock(), 0) + 60
                log.warning(
                    "sleep_until_reset", model=model, seconds=round(wait), why=record.done_reason
                )
                beat.sleeping(self.clock() + wait, record.done_reason or record.error_type)
                self.sleep(wait)
            elif record.termination == "api_failure":
                beat.sleeping(self.clock() + 60, record.error_type)
                self.sleep(60)  # per-minute exhaustion or an outage: brief pause, then retry

    def run(self, models: list[str] | None = None) -> None:
        """Run every model's worker, or only ``models`` (e.g. to start one model after its
        quota reset). The experiment, its cells and provenance are the same either way."""
        unknown = sorted(set(models or []) - set(self.experiment.models))
        if unknown:
            raise ValueError(f"models not in the experiment: {unknown}")
        selected = models or self.experiment.models
        pid_file = write_pid_file(self.experiment.run_dir, selected)
        threads = [
            threading.Thread(target=self.worker, args=(m,), name=f"worker-{m}", daemon=True)
            for m in selected
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            self.stop.set()
            raise
        finally:
            info = json.loads(pid_file.read_text(encoding="utf-8"))
            pid_file.write_text(json.dumps(info | {"ended_at": time.time()}), encoding="utf-8")
