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
    median_diff_lines: float | None = None
    median_test_seconds: float | None = None


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

    verified_ids = {t.task_id for t in verified}
    sizes = [c.changed_lines for c in candidates if c.task_id in verified_ids]
    if sizes:
        stats.median_diff_lines = statistics.median(sizes)
    runtimes = [m.oracle_seconds for m in metrics if m.task_id in verified_ids]
    if runtimes:
        stats.median_test_seconds = statistics.median(runtimes)
    return stats
