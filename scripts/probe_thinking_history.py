"""Can a model's earlier thinking be removed from the history, and does it then stop being billed?

Background (pilot, D13): on gemma-4-26b, reported prompt_tokens = countTokens + (thinking tokens
of earlier replies still in the prompt) + 1. The earlier thoughts travel as thought signatures.

Variants of the same third call, after two real tool-calling steps:
  A  full replay (baseline)
  B  older step's signature replaced by the documented dummy "skip_thought_signature_validator";
     the latest step keeps its real signature
  C  every signature replaced by the dummy
  D  older step's signature removed (no dummy)
  E  thought text parts dropped from every earlier reply, signatures kept
  F  thought text parts dropped from the older reply only

Records HTTP status, prompt_tokens and thinking per call to docs/week3/evidence/. Usage:
    uv run python scripts/probe_thinking_history.py --model gemma-4-26b-a4b-it
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import UTC, datetime

import yaml

from repogym.config import ROOT
from repogym.experiments.runner import LEDGER_PATH, gemini_api_key, make_provider
from repogym.providers.base import Message, ProviderError, ToolResult
from repogym.providers.retry import RetryPolicy
from repogym.ratelimit import LimitsConfig, QuotaLedger
from repogym.tools.definitions import TOOLS

DUMMY = "skip_thought_signature_validator"
SYSTEM = (
    "You are testing a tool interface. Think about which command is needed, then call exactly "
    "one tool per reply. Never answer in plain text."
)
TASK = (
    "Step 1: let N be the number of primes below 60 that are congruent to 3 mod 4; work it out "
    "carefully, then call bash with the command `echo N` (with N replaced by the number). "
    "Step 2: let M be the sum of the digits of 2**31; work it out carefully, then call bash with "
    "`echo M`. Step 3: call submit."
)


def signatures(message: Message, mode: str) -> Message:
    """A copy of an assistant message with its function-call signatures changed."""
    out = message.copy()
    raw = copy.deepcopy(message.raw)
    for part in raw.get("parts", []):
        if "thought_signature" in part:
            if mode == "dummy":
                part["thought_signature"] = DUMMY
            elif mode == "remove":
                del part["thought_signature"]
    if mode == "drop_thoughts":  # remove thought text parts, keep calls and their signatures
        raw["parts"] = [p for p in raw.get("parts", []) if not p.get("thought")]
    out.raw = raw
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemma-4-26b-a4b-it")
    args = parser.parse_args()
    settings = yaml.safe_load((ROOT / "configs/models.yaml").read_text())["models"][args.model]
    limits = LimitsConfig.load(ROOT / "configs/limits/gemini_free_tier.yaml")
    ledger = QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    provider = make_provider(args.model, settings, gemini_api_key(), None)
    provider.policy = RetryPolicy(max_attempts=3, base_delay=5.0)
    rows = []

    def call(label, history):
        ledger.add(args.model, time.time())
        row = {"variant": label, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        try:
            gen = provider.generate(SYSTEM, history, TOOLS, estimated_tokens=2000)
            row |= {
                "status": 200,
                "prompt_tokens": gen.usage.prompt_tokens,
                "thinking_tokens": gen.usage.thinking_tokens,
                "calls": [c.name for c in gen.message.calls],
                "attempts": [a.status for a in gen.attempts],
            }
        except ProviderError as exc:
            row |= {
                "status": exc.attempts[-1].status if exc.attempts else None,
                "error": str(exc)[:300],
                "attempts": [a.status for a in exc.attempts],
            }
            gen = None
        rows.append(row)
        print(json.dumps(row), flush=True)
        time.sleep(4)
        return gen

    history = [Message(role="user", text=TASK)]
    for output in ("ok\n", "ok\n"):
        gen = call(f"step{len(history) // 2 + 1}", history)
        if gen is None or not gen.message.calls:
            raise SystemExit("setup steps failed; see the rows above")
        history += [
            gen.message,
            Message(
                role="tool",
                results=[ToolResult(c.id, c.name, output) for c in gen.message.calls],
            ),
        ]
    first, second = history[1], history[3]
    raw_parts = [sorted(p) for p in first.raw.get("parts", [])]
    thought_chars = [
        sum(len(p.get("text", "")) for p in m.raw.get("parts", []) if p.get("thought"))
        for m in (first, second)
    ]
    variants = {
        "A_full": history,
        "B_older_dummy": [history[0], signatures(first, "dummy"), history[2], second, history[4]],
        "C_all_dummy": [
            history[0],
            signatures(first, "dummy"),
            history[2],
            signatures(second, "dummy"),
            history[4],
        ],
        "D_older_removed": [
            history[0],
            signatures(first, "remove"),
            history[2],
            second,
            history[4],
        ],
        "E_all_thoughts_dropped": [
            history[0],
            signatures(first, "drop_thoughts"),
            history[2],
            signatures(second, "drop_thoughts"),
            history[4],
        ],
        "F_older_thoughts_dropped": [
            history[0],
            signatures(first, "drop_thoughts"),
            history[2],
            second,
            history[4],
        ],
    }
    for label, view in variants.items():
        call(label, view)
    prior_thinking = [r.get("thinking_tokens") for r in rows[:2]]
    out = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": args.model,
        "assistant_part_keys": raw_parts,
        "prior_thinking_tokens": prior_thinking,
        "thought_text_chars_in_replayed_replies": thought_chars,
        "rows": rows,
    }
    path = ROOT / "docs/week3/evidence" / f"thinking_history_{args.model}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print("wrote", path)


if __name__ == "__main__":
    main()
