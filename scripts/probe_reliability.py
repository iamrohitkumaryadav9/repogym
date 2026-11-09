"""Reliability probe: small generation calls to one model, spread evenly over hours.

Each call is a single attempt (no retries), so the raw HTTP status is observed. Every call
is counted in the quota ledger and appended to a JSONL evidence file. Usage:

    uv run python scripts/probe_reliability.py --model gemma-4-31b-it --calls 20 --interval 2160
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from repogym.config import ROOT
from repogym.experiments.runner import LEDGER_PATH, gemini_api_key
from repogym.providers.base import ProviderError
from repogym.providers.gemini import GeminiProvider, GeminiSettings
from repogym.providers.retry import RetryPolicy
from repogym.ratelimit import LimitsConfig, QuotaLedger

PROMPT = "Reply with the single word OK."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemma-4-31b-it")
    parser.add_argument("--calls", type=int, default=20)
    parser.add_argument("--interval", type=float, default=2160.0)  # 36 min
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or ROOT / "docs/week3/evidence" / f"reliability_{args.model}.jsonl"
    settings = yaml.safe_load((ROOT / "configs/models.yaml").read_text())["models"][args.model]
    limits = LimitsConfig.load(ROOT / "configs/limits/gemini_free_tier.yaml")
    ledger = QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    provider = GeminiProvider(
        GeminiSettings(
            model=args.model,
            sampling=settings.get("sampling") or {},
            thinking_level=settings.get("thinking_level"),
        ),
        api_key=gemini_api_key(),
        policy=RetryPolicy(max_attempts=1),
    )
    start = time.time()
    for i in range(args.calls):
        due = start + i * args.interval
        time.sleep(max(due - time.time(), 0))
        at = datetime.now(UTC).isoformat(timespec="seconds")
        ledger.add(args.model, time.time())
        t0 = time.monotonic()
        row = {"i": i + 1, "at": at, "model": args.model}
        try:
            gen = provider.generate_text("You are terse.", PROMPT, args.max_tokens)
            row |= {
                "status": 200,
                "kind": "ok",
                "finish_reason": gen.finish_reason,
                "prompt_tokens": gen.usage.prompt_tokens,
                "thinking_tokens": gen.usage.thinking_tokens,
            }
        except ProviderError as exc:
            last = exc.attempts[-1] if exc.attempts else None
            row |= {
                "status": last.status if last else None,
                "kind": last.kind if last else type(exc).__name__,
                "error": type(exc).__name__,
            }
        row["latency_seconds"] = round(time.monotonic() - t0, 2)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
