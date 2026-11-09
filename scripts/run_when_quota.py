"""Start an experiment for one model once (a) another experiment's episodes for that model are
recorded and (b) the model has enough of today's quota left; otherwise wait for its reset.

Our local ledger is the source of truth for our own requests (D20). Decisions are printed with
timestamps, so the log shows exactly when and why the run started. Usage:

    uv run python scripts/run_when_quota.py \
        --experiment configs/experiments/flash_thinking_high.yaml \
        --model gemini-3.5-flash-lite --need 240 \
        --after configs/experiments/repilot.yaml --after-records 3
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from repogym.config import ROOT
from repogym.experiments import spec
from repogym.experiments.runner import LEDGER_PATH, latest_records
from repogym.ratelimit import LimitsConfig, QuotaLedger


def say(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


def recorded(experiment: spec.Experiment, model: str) -> int:
    return sum(
        len(v) for k, v in latest_records(experiment.run_dir).items() if k.startswith(f"{model}__")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--need", type=int, required=True, help="requests the run may need")
    parser.add_argument("--after", type=Path, help="experiment that must finish first")
    parser.add_argument("--after-records", type=int, default=0)
    parser.add_argument("--poll", type=float, default=300.0)
    args = parser.parse_args()
    experiment = spec.load(args.experiment)
    limits = LimitsConfig.load(experiment.limits_path)
    ledger = QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    rpd = limits.effective(args.model)["rpd"]
    if args.after:
        before = spec.load(args.after)
        while recorded(before, args.model) < args.after_records:
            time.sleep(args.poll)
        say(f"{before.name}: {args.model} has {recorded(before, args.model)} records; go on")
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
