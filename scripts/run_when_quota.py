"""Start an experiment for one model once (a) another experiment's cells for that model are all
done and (b) the model has at least ``--need`` requests of today's quota left (default 0: start
at once, and let the runner hold an episode across the reset if the quota runs out, D16).

Our local ledger is the source of truth for our own requests (D20). Decisions are printed with
timestamps, so the log shows exactly when and why the run started. Usage:

    uv run python scripts/run_when_quota.py \
        --experiment configs/experiments/flash_thinking_high.yaml \
        --model gemini-3.5-flash-lite --after configs/experiments/repilot.yaml
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from repogym.config import ROOT
from repogym.experiments import spec
from repogym.experiments.runner import LEDGER_PATH, cell_done, latest_records
from repogym.ratelimit import LimitsConfig, QuotaLedger


def say(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


def all_done(experiment: spec.Experiment, model: str) -> bool:
    records = latest_records(experiment.run_dir)
    return all(cell_done(records.get(c.key, [])) for c in experiment.cells() if c.model == model)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--need", type=int, default=0, help="requests that must be left today")
    parser.add_argument("--after", type=Path, help="experiment whose cells must finish first")
    parser.add_argument("--poll", type=float, default=300.0)
    args = parser.parse_args()
    experiment = spec.load(args.experiment)
    limits = LimitsConfig.load(experiment.limits_path)
    ledger = QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    rpd = limits.effective(args.model)["rpd"]
    if args.after:
        before = spec.load(args.after)
        while not all_done(before, args.model):
            time.sleep(args.poll)
        say(f"{before.name}: all {args.model} cells are done; go on")
    while True:
        now = time.time()
        used = ledger.requests(args.model, now)
        if rpd - used >= args.need:
            say(f"{args.model}: {used}/{rpd} used today, {rpd - used} left >= {args.need}; start")
            break
        reset = ledger.next_reset(now) + 300
        say(
            f"{args.model}: {used}/{rpd} used today, {rpd - used} left < {args.need}; wait for "
            f"the reset, until {datetime.fromtimestamp(reset, UTC).isoformat(timespec='seconds')}"
        )
        time.sleep(max(reset - time.time(), 0))
    os.chdir(ROOT)
    uv = Path.home() / ".local/bin/uv"
    os.execv(
        str(uv),
        [
            str(uv),
            "run",
            "repogym",
            "run",
            "--experiment",
            str(args.experiment),
            "--models",
            args.model,
        ],
    )


if __name__ == "__main__":
    main()
