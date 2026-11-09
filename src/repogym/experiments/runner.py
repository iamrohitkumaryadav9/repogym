"""Run an experiment: one worker per model, in parallel, resumable and idempotent.

* Each model has its own provider and rate limiter. All models share one quota ledger on disk
  (D11), so throughput is governed per model and a model can't exceed its daily quota across
  restarts.
* A cell (model, strategy, task, repeat) is done once its latest episode record exists and isn't
  an ``api_failure``. Episodes interrupted by a crash, sleep or reboot leave no record and simply
  rerun.
* Pre-registered exclusion rules:
  * a ``harness_error`` episode is rerun exactly once, and both records are kept and reported;
  * an ``api_failure`` caused by daily quota exhaustion is retried after that model's reset,
    while the worker sleeps and the other models keep running.
* The guard (test-split protection, pilot hygiene) runs before anything starts.
"""

from __future__ import annotations

import json
import os
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
from repogym.harness import EpisodeRecord, run_episode
from repogym.providers.gemini import GeminiProvider, GeminiSettings
from repogym.ratelimit import LimitsConfig, QuotaLedger, RateLimiter

log = structlog.get_logger()
LEDGER_PATH = DATA_DIR / "quota" / "ledger.json"
HARNESS_ERROR_RERUNS = 1


def gemini_api_key() -> str:
    """GEMINI_API_KEY from the environment or .env; the only setting Week 3 runs need."""
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (in the environment or .env)")
    return key


def latest_records(run_dir: Path) -> dict[str, list[dict]]:
    path = run_dir / "episodes.jsonl"
    out: dict[str, list[dict]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                out.setdefault(record.get("episode_key") or record["task_id"], []).append(record)
    return out


def cell_done(records: list[dict]) -> bool:
    """Done unless never run, the latest was an api_failure, or it needs its one rerun."""
    if not records:
        return False
    latest = records[-1].get("termination")
    if latest == "api_failure":
        return False
    if latest == "harness_error":
        reruns = sum(r.get("termination") == "harness_error" for r in records)
        return reruns > HARNESS_ERROR_RERUNS
    return True


def provenance(experiment: Experiment, cell: Cell) -> dict:
    return {
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
    }


def make_provider(model: str, settings: dict, api_key: str, limiter):
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
    )


def make_agent(experiment: Experiment, cell: Cell, provider) -> ToolAgent:
    counting = experiment.model_settings[cell.model].get("token_counting", "estimate")
    counter = provider.count_tokens if counting == "count_tokens" else None
    cm = ContextManager(cell.strategy, experiment.context, TokenEstimator(), counter=counter)
    return ToolAgent(
        provider,
        cm,
        shadow_count=counting == "shadow",
        settings=experiment.model_settings[cell.model],
    )


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
    ) -> None:
        guard_fn(experiment, final_test_run)
        self.experiment, self.api_key = experiment, api_key
        self.limits = LimitsConfig.load(experiment.limits_path)
        self.ledger = ledger or QuotaLedger(LEDGER_PATH, self.limits.reset_timezone)
        self.episode_fn, self.provider_fn = episode_fn, provider_fn
        self.clock, self.sleep, self.on_done = clock, sleep, on_done
        self.tasks = release_tasks(experiment.release)
        self.stop = threading.Event()

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
            model, self.experiment.model_settings[model], self.api_key, limiter
        )
        while not self.stop.is_set():
            cells = self.pending(model)
            if not cells:
                return
            cell = cells[0]
            record = self.episode_fn(
                self.tasks[cell.task_id],
                make_agent(self.experiment, cell, provider),
                self.experiment.run_dir,
                self.experiment.budgets,
                release=self.experiment.release,
                api_tier="free",
                episode_key=cell.key,
                experiment=provenance(self.experiment, cell),
            )
            if self.on_done:
                self.on_done(record)
            if record.error_type == "DailyQuotaExhausted":
                reset = self.ledger.next_reset(self.clock())
                wait = max(reset - self.clock(), 0) + 60
                log.warning("daily_quota_sleep", model=model, seconds=round(wait))
                self.sleep(wait)
            elif record.termination == "api_failure":
                self.sleep(60)  # per-minute exhaustion or an outage: brief pause, then retry

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.worker, args=(m,), name=f"worker-{m}", daemon=True)
            for m in self.experiment.models
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            self.stop.set()
            raise
