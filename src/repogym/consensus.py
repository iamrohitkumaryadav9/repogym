"""Three independent classifier runs per task; a label is applied only on a confident consensus.

Temperature 0 is not fully deterministic on Gemini (2 of 79 calibration labels flipped between
two runs), so a single answer isn't trusted. Each run has its own cache key, so it really is a
separate request, and a rerun replays successful runs from the cache for free. Runs are appended
to a crash-safe JSONL file as they finish: a rerun skips runs that already succeeded and retries
the ones that failed, so the job can span several sessions under free-tier limits.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from repogym.classify import TaskTypeLabel, classify
from repogym.llm import GeminiClient
from repogym.models import Candidate

RUNS = 3
MIN_CONFIDENCE = 0.9
STOP_AFTER_UNAVAILABLE = 3  # consecutive exhausted-retry failures: probably a quota; stop

Outcome = Literal["applied", "disagreement", "low_confidence", "failed", "incomplete"]


@dataclass(frozen=True)
class Consensus:
    task_id: str
    outcome: Outcome
    task_type: str  # the agreed label if applied, otherwise "unknown"
    runs: tuple[TaskTypeLabel, ...]


def decide(task_id: str, runs: list[TaskTypeLabel], n_runs: int = RUNS,
           min_confidence: float = MIN_CONFIDENCE) -> Consensus:  # fmt: skip
    """All ``n_runs`` must succeed, agree, and each be at least ``min_confidence``."""
    ordered = tuple(sorted(runs, key=lambda r: r.run or 0))
    if len(ordered) < n_runs:
        return Consensus(task_id, "incomplete", "unknown", ordered)
    if any(r.task_type == "unknown" for r in ordered):
        return Consensus(task_id, "failed", "unknown", ordered)
    if len({r.task_type for r in ordered}) > 1:
        return Consensus(task_id, "disagreement", "unknown", ordered)
    if any((r.confidence or 0) < min_confidence for r in ordered):
        return Consensus(task_id, "low_confidence", "unknown", ordered)
    return Consensus(task_id, "applied", ordered[0].task_type, ordered)


def latest_runs(records: Iterable[TaskTypeLabel]) -> dict[str, dict[int, TaskTypeLabel]]:
    """The last record per (task, run): a later retry of a failed run replaces it."""
    runs: dict[str, dict[int, TaskTypeLabel]] = {}
    for record in records:
        if record.run is not None:
            runs.setdefault(record.task_id, {})[record.run] = record
    return runs


def classify_runs(
    candidates: list[Candidate],
    client: GeminiClient,
    done: dict[str, dict[int, TaskTypeLabel]],
    save: Callable[[TaskTypeLabel], None],
    n_runs: int = RUNS,
    stop_after_unavailable: int = STOP_AFTER_UNAVAILABLE,
) -> bool:
    """Run every missing or failed (task, run). Returns False if it stopped early on a quota."""
    unavailable = 0
    for candidate in candidates:
        for run in range(n_runs):
            previous = done.get(candidate.task_id, {}).get(run)
            if previous is not None and previous.task_type != "unknown":
                continue
            label = classify(candidate, client, run=run)
            save(label)
            done.setdefault(candidate.task_id, {})[run] = label
            unavailable = unavailable + 1 if "attempts failed" in label.error else 0
            if unavailable >= stop_after_unavailable:
                return False
    return True


@dataclass(frozen=True)
class Stability:
    tasks: int  # tasks with all runs successful
    all_agree: int
    pairs: int
    pairs_agree: int

    @property
    def task_rate(self) -> float | None:
        return self.all_agree / self.tasks if self.tasks else None

    @property
    def pair_rate(self) -> float | None:
        return self.pairs_agree / self.pairs if self.pairs else None


def stability(results: Iterable[Consensus]) -> Stability:
    """Run-to-run agreement over tasks whose runs all succeeded."""
    tasks = all_agree = pairs = pairs_agree = 0
    for result in results:
        if result.outcome in ("incomplete", "failed"):
            continue
        types = [r.task_type for r in result.runs]
        tasks += 1
        all_agree += len(set(types)) == 1
        for a, b in combinations(types, 2):
            pairs += 1
            pairs_agree += a == b
    return Stability(tasks, all_agree, pairs, pairs_agree)


def summarize(results: list[Consensus]) -> list[str]:
    outcomes = Counter(r.outcome for r in results)
    applied = Counter(r.task_type for r in results if r.outcome == "applied")
    s = stability(results)
    rate = "n/a" if s.task_rate is None else f"{s.task_rate:.3f}"
    pair = "n/a" if s.pair_rate is None else f"{s.pair_rate:.3f}"
    return [
        f"{len(results)} tasks: "
        + ", ".join(
            f"{k}={outcomes[k]}"
            for k in ("applied", "disagreement", "low_confidence", "failed", "incomplete")
        ),  # fmt: skip
        "applied labels: " + (", ".join(f"{k}={v}" for k, v in sorted(applied.items())) or "none"),
        f"run-to-run agreement: all {RUNS} runs agree on {s.all_agree}/{s.tasks} tasks ({rate}); "
        f"pairwise {s.pairs_agree}/{s.pairs} ({pair})",
    ]
