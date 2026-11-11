"""`repogym status`: per-model progress of an experiment, quota use today, tokens, solves, ETA."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime

from repogym.experiments.runner import (
    LEDGER_PATH,
    MAX_API_FAILURES,
    cell_done,
    counted_api_failures,
    latest_records,
)
from repogym.experiments.spec import Experiment
from repogym.ratelimit import LimitsConfig, QuotaLedger


def _seconds(record: dict) -> float | None:
    try:
        start = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def summary(
    experiment: Experiment, now: float | None = None, ledger: QuotaLedger | None = None
) -> list[dict]:
    now = time.time() if now is None else now
    limits = LimitsConfig.load(experiment.limits_path)
    ledger = ledger or QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    records = latest_records(experiment.run_dir)
    rows = []
    for model in experiment.models:
        cells = [c for c in experiment.cells() if c.model == model]
        done = [c for c in cells if cell_done(records.get(c.key, []))]
        finals = [records[c.key][-1] for c in done]
        durations = [d for r in finals if (d := _seconds(r))]
        mean = sum(durations) / len(durations) if durations else None
        remaining = len(cells) - len(done)
        attempts = {c.key: records.get(c.key, []) for c in cells}
        rows.append(
            {
                "model": model,
                "done": len(done),
                "total": len(cells),
                "solved": sum(r.get("reward") == 1 for r in finals),
                "terminations": dict(Counter(r.get("termination") for r in finals)),
                "requests_today": ledger.requests(model, now),
                "rpd_limit": limits.effective(model)["rpd"],
                "quota_exhausted_today": ledger.exhausted(model, now),
                "input_tokens": sum(
                    (r.get("accounting") or {}).get("input_tokens", 0) for r in finals
                ),
                "output_tokens": sum(
                    (r.get("accounting") or {}).get("output_tokens", 0)
                    + (r.get("accounting") or {}).get("thinking_tokens", 0)
                    for r in finals
                ),
                "budget_violations": sum(
                    (r.get("accounting") or {}).get("budget_violations", 0) for r in finals
                ),
                "attempts": sum(len(v) for v in attempts.values()),
                "cells_retried": sum(len(v) > 1 for v in attempts.values()),
                "cells_given_up": sum(
                    counted_api_failures(v) >= MAX_API_FAILURES for v in attempts.values()
                ),
                "mean_episode_minutes": round(mean / 60, 1) if mean else None,
                "eta_hours": round(remaining * mean / 3600, 1) if mean else None,
            }
        )
    return rows
