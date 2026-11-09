"""Per-episode token, request and cost accounting from the trajectory's step and event records.

Cost is a list-price equivalent (configs/prices.yaml); the free tier bills nothing. For models
without a comparable list price (Gemma on this API), cost is None and only tokens are reported.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PRICES_PATH = Path(__file__).resolve().parents[2] / "configs" / "prices.yaml"


def load_prices(path: Path = PRICES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def call_cost(
    model: str, prompt: int, cached: int, output: int, thinking: int, prices: dict
) -> float | None:
    price = (prices.get("usd_per_million_tokens") or {}).get(model)
    if price is None:
        return None
    uncached = max(prompt - cached, 0)
    return (
        uncached * price["input"]
        + cached * price.get("cached_input", price["input"])
        + (output + thinking) * price["output_including_thinking"]
    ) / 1e6


def _sum(calls: list[dict], key: str) -> int:
    return sum(c.get(key) or 0 for c in calls)


def summarize(steps, events, model: str | None, prices: dict | None = None) -> dict:
    """Totals over every LLM call in an episode: agent calls (on steps) and summary calls
    (on events). Summary overhead is reported separately as well."""
    prices = load_prices() if prices is None else prices
    agent_calls = [
        s.metrics["extra"]["repogym"]
        | {
            "prompt_tokens": s.metrics.get("prompt_tokens"),
            "cached_tokens": s.metrics.get("cached_tokens"),
        }
        for s in steps
        if s.metrics and "extra" in s.metrics
    ]
    summary_calls = [e.metrics for e in events if e.kind == "summary" and e.metrics]
    calls = agent_calls + summary_calls

    def totals(group: list[dict]) -> dict:
        prompt, cached = _sum(group, "prompt_tokens"), _sum(group, "cached_tokens")
        output, thinking = _sum(group, "output_tokens"), _sum(group, "thinking_tokens")
        costs = (
            [
                call_cost(
                    model,
                    c.get("prompt_tokens") or 0,
                    c.get("cached_tokens") or 0,
                    c.get("output_tokens") or 0,
                    c.get("thinking_tokens") or 0,
                    prices,
                )
                for c in group
            ]
            if model
            else []
        )
        return {
            "calls": len(group),
            "requests": sum(c.get("attempts", 1) for c in group),
            "input_tokens": prompt,
            "cached_tokens": cached,
            "output_tokens": output,
            "thinking_tokens": thinking,
            "total_tokens": prompt + output + thinking,
            "cost_usd": None
            if not costs or any(c is None for c in costs)
            else round(sum(costs), 6),
        }

    errors = [c["estimation_error"] for c in agent_calls if c.get("estimation_error") is not None]
    return {
        **totals(calls),
        "summary_overhead": totals(summary_calls),
        "budget_violations": sum(bool(c.get("budget_violation")) for c in agent_calls),
        "estimation_error_mean": round(sum(errors) / len(errors), 4) if errors else None,
        "estimation_error_max_under": round(min(errors), 4) if errors else None,
        "retries": sum(c.get("retries", 0) for c in calls),
        "rate_limited_attempts": sum(c.get("rate_limited", 0) for c in calls),
        # Time inside provider calls not spent on the API itself: limiter waits and retry
        # backoff. It counts toward the wall-clock cap (D17).
        "waiting_seconds": round(sum(c.get("waiting_seconds") or 0.0 for c in calls), 1),
        "api_seconds": round(sum(c.get("latency_seconds") or 0.0 for c in calls), 1),
    }
