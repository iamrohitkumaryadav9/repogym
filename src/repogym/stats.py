"""Aggregate pipeline outputs for ``repogym stats``."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from repogym.config import (
    BASELINE_PATH,
    CANDIDATES_PATH,
    REJECTIONS_PATH,
    RUN_METRICS_PATH,
    VERIFIED_PATH,
)
from repogym.models import BaselineRecord, Candidate, Rejection, RunMetrics, VerifiedTask
from repogym.validate import read_jsonl


@dataclass
class Stats:
    candidates_per_repo: Counter[str] = field(default_factory=Counter)
    verified_per_repo: Counter[str] = field(default_factory=Counter)
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    rejections_per_repo: Counter[str] = field(default_factory=Counter)
    baseline_failing_tests: Counter[str] = field(default_factory=Counter)
    tasks_with_baseline_failures: Counter[str] = field(default_factory=Counter)
    breakdown: dict[tuple[str, str], Counter[bool]] = field(default_factory=dict)
    fail_to_pass_buckets: dict[str, int] = field(default_factory=dict)
    median_diff_lines: float | None = None
    median_test_seconds: float | None = None


def fail_to_pass_buckets(counts: list[int]) -> dict[str, int]:
    """Single-test tasks give a coarser reward signal, so report how common they are."""
    return {
        "1": sum(n == 1 for n in counts),
        "2-5": sum(2 <= n <= 5 for n in counts),
        "6+": sum(n >= 6 for n in counts),
    }


def collect() -> Stats:
    candidates = read_jsonl(CANDIDATES_PATH, Candidate)
    verified = read_jsonl(VERIFIED_PATH, VerifiedTask)
    rejections = read_jsonl(REJECTIONS_PATH, Rejection)
    baselines = read_jsonl(BASELINE_PATH, BaselineRecord)
    metrics = read_jsonl(RUN_METRICS_PATH, RunMetrics)

    stats = Stats(
        candidates_per_repo=Counter(c.repo for c in candidates),
        verified_per_repo=Counter(t.repo for t in verified),
        rejection_reasons=Counter(r.reason for r in rejections),
        rejections_per_repo=Counter(r.repo for r in rejections),
    )
    for record in baselines:
        if record.failing or record.collection_errors:
            stats.tasks_with_baseline_failures[record.repo] += 1
        stats.baseline_failing_tests.update(record.failing)

    for task in verified:
        stats.breakdown.setdefault((task.repo, task.task_type), Counter())[task.spec_clear] += 1
    stats.fail_to_pass_buckets = fail_to_pass_buckets([len(t.fail_to_pass) for t in verified])

    verified_ids = {t.task_id for t in verified}
    sizes = [c.changed_lines for c in candidates if c.task_id in verified_ids]
    if sizes:
        stats.median_diff_lines = statistics.median(sizes)
    runtimes = [
        m.oracle_seconds
        for m in metrics
        if m.task_id in verified_ids and m.oracle_seconds is not None
    ]
    if runtimes:
        stats.median_test_seconds = statistics.median(runtimes)
    return stats
