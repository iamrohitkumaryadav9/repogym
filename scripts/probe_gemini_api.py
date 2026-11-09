"""Measure Gemini API behaviour the docs don't state (evidence for DECISIONS.md, Gate 1).

    uv run python scripts/probe_gemini_api.py [--model gemma-4-26b-a4b-it] [--count-burst 35]

1. countTokens vs rate limits: a burst of countTokens calls above the model's RPM, then
   countTokens over more input tokens than its TPM, each followed by a tiny generateContent
   call. A 429 on countTokens, or a generation 429 right after, means countTokens is counted.
2. Thinking defaults: the same short prompt with no thinking config and with each documented
   setting, recording thoughts_token_count and any error.

Writes docs/week3/evidence/api_probes_<model>.json. Never prints the API key.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from google import genai
from google.genai import errors, types

from repogym.llm import Settings

ROOT = Path(__file__).resolve().parents[1]
NO_RETRY = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def call(fn):
    start = time.monotonic()
    try:
        out = fn()
        return {"ok": True, "seconds": round(time.monotonic() - start, 2), "result": out}
    except errors.APIError as exc:
        return {"ok": False, "seconds": round(time.monotonic() - start, 2), "status": exc.code,
                "error": (exc.status or "")}


def tiny_generation(client, model):
    def go():
        r = client.models.generate_content(model=model, contents="Reply with the word OK.",
                                           config=types.GenerateContentConfig(http_options=NO_RETRY))
        u = r.usage_metadata
        return {"prompt": u.prompt_token_count, "output": u.candidates_token_count,
                "thoughts": u.thoughts_token_count}
    return call(go)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemma-4-26b-a4b-it")
    parser.add_argument("--count-burst", type=int, default=35)
    parser.add_argument("--tpm-text-tokens", type=int, default=12000)
    args = parser.parse_args()
    client = genai.Client(api_key=Settings.from_env().api_key, http_options=NO_RETRY)
    model = args.model
    out: dict = {"model": model, "started_at": now(), "sdk": genai.__version__}

    # 1a. countTokens burst above RPM.
    burst = []
    t0 = time.monotonic()
    for _ in range(args.count_burst):
        burst.append(call(lambda: client.models.count_tokens(model=model, contents="hello").total_tokens))
    out["count_burst"] = {"calls": len(burst), "seconds": round(time.monotonic() - t0, 1),
                          "errors": [b for b in burst if not b["ok"]][:5],
                          "n_errors": sum(not b["ok"] for b in burst)}
    out["generation_after_count_burst"] = tiny_generation(client, model)

    # 1b. countTokens over more input tokens than the TPM limit, within a minute.
    words = " ".join(f"word{i}" for i in range(args.tpm_text_tokens // 2))
    counts = []
    t0 = time.monotonic()
    for _ in range(3):
        counts.append(call(lambda: client.models.count_tokens(model=model, contents=words).total_tokens))
    out["count_tpm"] = {"calls": counts, "seconds": round(time.monotonic() - t0, 1),
                        "tokens_counted": sum(c.get("result") or 0 for c in counts)}
    out["generation_after_count_tpm"] = tiny_generation(client, model)

    # 2. Thinking settings.
    prompt = "What is 17 * 23? Answer with the number only."
    variants = {
        "none": None,
        "level_minimal": types.ThinkingConfig(thinking_level="minimal"),
        "level_low": types.ThinkingConfig(thinking_level="low"),
        "level_high": types.ThinkingConfig(thinking_level="high"),
        "budget_0": types.ThinkingConfig(thinking_budget=0),
        "budget_dynamic": types.ThinkingConfig(thinking_budget=-1),
    }
    thinking = {}
    for name, cfg in variants.items():
        def go(cfg=cfg):
            r = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(thinking_config=cfg, http_options=NO_RETRY))
            u = r.usage_metadata
            return {"thoughts": u.thoughts_token_count, "output": u.candidates_token_count,
                    "text": (r.text or "")[:40], "finish": r.candidates[0].finish_reason.name
                    if r.candidates else None, "model_version": r.model_version}
        thinking[name] = call(go)
        time.sleep(3)
    out["thinking"] = thinking
    out["ended_at"] = now()
    dest = ROOT / "docs" / "week3" / "evidence" / f"api_probes_{model}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
