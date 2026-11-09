"""API reliability per model by UTC hour: attempts, server errors (5xx and transport), rate.

Read-only. Sources, in order of precedence (no attempt is counted twice):
1. data/quota/attempts-*.jsonl: every attempt with its time (D16). Authoritative from its first
   row onwards.
2. Before that: per-call attempt logs in experiment trajectories (timed by the step), plus the
   failed attempts behind episodes that ended in ProviderUnavailable (not in trajectories; the
   old policy made exactly 6, timed by the record's end).
3. Probe evidence: the reliability probe and the thinking-history runs.

    python3 scripts/reliability_by_hour.py [run-dir ...]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ["week3-pilot", "week3-repilot", "week3-flash-thinking-high"]
OLD_POLICY_FAILED_ATTEMPTS = 6


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def is_error(status) -> bool:
    return status is None or (isinstance(status, int) and status >= 500)


def collect() -> list[tuple[float, str, int | None, str]]:
    """(time, model, status, source) for every known attempt."""
    rows: list[tuple[float, str, int | None, str]] = []
    logs = sorted((ROOT / "data/quota").glob("attempts-*.jsonl"))
    log_rows = [json.loads(x) for f in logs for x in f.read_text().splitlines() if x.strip()]
    log_start = min((r["at"] for r in log_rows), default=float("inf"))
    rows += [
        (r["at"], r["model"], r["status"], "attempt-log")
        for r in log_rows
        if r["kind"] != "daily_quota_pause"
    ]
    run_dirs = [Path(a) for a in sys.argv[1:]] or [ROOT / "data/runs" / r for r in RUNS]
    for run_dir in run_dirs:
        for path in sorted((run_dir / "trajectories").glob("*.json")) if run_dir.exists() else []:
            model = path.name.split("__")[0]
            for step in json.loads(path.read_text())["steps"]:
                metrics = step.get("metrics")
                if step.get("source") != "agent" or not metrics:
                    continue
                at = ts(step["timestamp"])
                if at >= log_start:
                    continue
                for attempt in metrics["extra"]["repogym"].get("attempt_log") or []:
                    rows.append((at, model, attempt.get("status"), "trajectory"))
        records = run_dir / "episodes.jsonl"
        for line in records.read_text().splitlines() if records.exists() else []:
            r = json.loads(line)
            if r.get("error_type") == "ProviderUnavailable" and r.get("ended_at"):
                at = ts(r["ended_at"])
                if at < log_start:
                    model = (r.get("episode_key") or "").split("__")[0]
                    rows += [(at, model, 500, "failed-call")] * OLD_POLICY_FAILED_ATTEMPTS
    evidence = ROOT / "docs/week3/evidence"
    for path in evidence.glob("reliability_*.jsonl"):
        for line in path.read_text().splitlines():
            r = json.loads(line)
            rows.append((ts(r["at"]), r["model"], r.get("status"), "probe"))
    for path in evidence.glob("thinking_history_*.json"):
        doc = json.loads(path.read_text())
        for r in doc["rows"]:
            for status in r.get("attempts") or []:
                rows.append((ts(r["at"]), doc["model"], status, "probe"))
    return rows


def main() -> None:
    table: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for at, model, status, _ in collect():
        hour = datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:00")
        table[(model, hour)][0] += 1
        table[(model, hour)][1] += is_error(status)
    print("| Model | Hour (UTC) | Attempts | Server errors | Rate |")
    print("|---|---|---|---|---|")
    for (model, hour), (n, e) in sorted(table.items()):
        print(f"| {model} | {hour} | {n} | {e} | {e / n:.0%} |")
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (model, _), (n, e) in table.items():
        totals[model][0] += n
        totals[model][1] += e
    for model, (n, e) in sorted(totals.items()):
        print(f"\n{model}: {e}/{n} attempts were server errors ({e / n:.0%})", end="")
    print()


if __name__ == "__main__":
    main()
