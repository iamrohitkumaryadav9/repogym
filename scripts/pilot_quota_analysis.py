"""Per-minute input tokens from pilot trajectories, against the free-tier TPM limits.

Read-only. For every model call in the pilot trajectories: its time (the step timestamp minus
the call's API latency) and its reported prompt_tokens. Prints, per model:
* the peak rolling-60-second sum of reported input tokens (what Google's TPM counts, D11);
* the peak of what our limiter admitted on (its pre-call estimates);
* for calls that hit 5xx before succeeding, how full the preceding 60 s window was, compared
  with calls that didn't.

    python3 scripts/pilot_quota_analysis.py [run-dir]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPM = {"gemini-3.5-flash-lite": 250_000, "gemma-4-26b-a4b-it": 16_000, "gemma-4-31b-it": 16_000}


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def calls(run_dir: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for path in sorted((run_dir / "trajectories").glob("*.json")):
        model = path.name.split("__")[0]
        doc = json.loads(path.read_text())
        for step in doc["steps"]:
            m = step.get("metrics")
            if step.get("source") != "agent" or not m:
                continue
            x = m["extra"]["repogym"]
            out.setdefault(model, []).append(
                {
                    "t": ts(step["timestamp"]) - (x.get("latency_seconds") or 0.0),
                    "reported": m.get("prompt_tokens") or 0,
                    "estimated": x.get("estimated_tokens") or 0,
                    "server_errors": sum(
                        1 for a in x.get("attempt_log") or [] if (a.get("status") or 0) >= 500
                    ),
                    "file": path.name,
                }
            )
    for rows in out.values():
        rows.sort(key=lambda r: r["t"])
    return out


def window(rows: list[dict], key: str, end: float, include_end: bool = True) -> int:
    return sum(r[key] for r in rows if end - 60 < r["t"] <= end and (include_end or r["t"] < end))


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/runs/week3-pilot"
    for model, rows in calls(run_dir).items():
        peak_reported = max(window(rows, "reported", r["t"]) for r in rows)
        peak_estimated = max(window(rows, "estimated", r["t"]) for r in rows)
        limit = TPM.get(model)
        over = [r for r in rows if limit and window(rows, "reported", r["t"]) > limit]
        print(
            f"{model}: {len(rows)} calls | peak 60 s input, reported {peak_reported:,}, "
            f"admitted-on estimate {peak_estimated:,} | TPM limit {limit:,} | "
            f"calls ending a window over the limit: {len(over)}"
        )
        buckets: dict[int, int] = {}
        for r in rows:  # calendar minutes, as a per-minute dashboard would count them
            buckets[int(r["t"] // 60)] = buckets.get(int(r["t"] // 60), 0) + r["reported"]
        top = max(buckets.values())
        single = max(r["reported"] for r in rows)
        print(
            f"    peak calendar-minute input {top:,} | largest single call {single:,} | "
            f"minutes over the limit {sum(v > limit for v in buckets.values())}/{len(buckets)}"
        )
        errs = [r for r in rows if r["server_errors"]]
        if errs:

            def before(r, rows=rows):  # input already sent in this call's calendar minute
                minute = int(r["t"] // 60)
                return sum(
                    x["reported"] for x in rows if int(x["t"] // 60) == minute and x["t"] < r["t"]
                )

            with_e = sorted(before(r) for r in errs)
            without = sorted(before(r) for r in rows if not r["server_errors"])
            over_e = sum(v + 0 > limit for v in with_e)
            over_n = sum(v > limit for v in without)
            print(
                f"    calls with 5xx before success: {len(errs)} "
                f"(median preceding-60 s input {with_e[len(with_e) // 2]:,}, over limit "
                f"{over_e}/{len(with_e)}) | without: {len(without)} (median "
                f"{without[len(without) // 2]:,}, over limit {over_n}/{len(without)})"
            )


if __name__ == "__main__":
    main()
