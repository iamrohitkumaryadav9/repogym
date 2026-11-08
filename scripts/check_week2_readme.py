"""Check that every Week 2 number in the README matches the committed evidence in data/runs/.

    uv run python scripts/check_week2_readme.py

Recomputes each figure from the episode records and trajectories, and fails if the README's
Week 2 section doesn't state it, or if any evidence episode isn't from one clean commit.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from repogym.agents.scripted import plan_attacks
from repogym.env.environment import load_task
from repogym.expectations import check

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "runs"
EVIDENCE = {
    "dev-oracle": "clean3-dev-oracle",
    "dev-null": "clean3-dev-null",
    "dev-cheater": "clean3-dev-cheater",
    "all-oracle": "clean3-all-oracle",
    "all-null": "clean3-all-null",
    "test-cheater": "clean3-test-cheater",
    "baseline": "clean4-smoke-baseline",
}


def records(run: str) -> list[dict]:
    rows = [json.loads(line) for line in (RUNS / run / "episodes.jsonl").read_text().splitlines()]
    return list({r["task_id"]: r for r in rows}.values())


def week2(readme: str) -> str:
    start = readme.index("## Week 2: Environment and harness")
    return readme[start : readme.index("\n## ", start + 10)]


def main() -> int:
    text = week2((ROOT / "README.md").read_text(encoding="utf-8"))
    problems: list[str] = []

    def need(snippet: str, why: str) -> None:
        if snippet not in text:
            problems.append(f"README lacks {snippet!r} ({why})")

    commits: dict[str, set[str]] = {"scripted": set(), "baseline": set()}
    dirty = set()
    for key, run in EVIDENCE.items():
        rs = records(run)
        group = "baseline" if key == "baseline" else "scripted"
        commits[group] |= {r["repogym_commit"] for r in rs}
        dirty |= {r["repogym_dirty"] for r in rs}
        if key == "baseline" and {r.get("api_tier") for r in rs} != {"free"}:
            problems.append("baseline episodes don't all record api_tier=free")
        if any(r["status"] != "graded" for r in rs):
            problems.append(f"{run}: not every episode graded")
        agent = key.split("-")[-1]
        if agent in ("oracle", "null", "cheater"):

            def trajectory(r: dict, run: str = run) -> dict | None:
                path = RUNS / run / r["trajectory"]
                return json.loads(path.read_text()) if path.exists() else None

            bad = [
                r["task_id"] for r in rs if check(agent, r, load_task(r["task_id"]), trajectory(r))
            ]
            if bad:
                problems.append(f"{run}: expectation failures {bad[:5]}")
    for key, expect in (
        ("dev-oracle", "**25/25**"),
        ("dev-null", "**0/25**"),
        ("dev-cheater", "**0/25**"),
        ("all-oracle", "**247/247**"),
        ("all-null", "**0/247**"),
        ("test-cheater", "**0/48**"),
    ):
        rs = records(EVIDENCE[key])
        solved = sum(r["reward"] == 1 for r in rs)
        if f"**{solved}/{len(rs)}**" != expect:
            problems.append(f"{key}: evidence says {solved}/{len(rs)}, README table says {expect}")
        need(expect, key)
    oracle_all = records(EVIDENCE["all-oracle"])
    if any(r["flags_raised"] for r in oracle_all):
        problems.append("all-oracle has flags, README says none")

    # Per-attack table (dev cheater).
    rows: dict[str, list[int]] = {}
    for r in records(EVIDENCE["dev-cheater"]):
        task = load_task(r["task_id"])
        traj = json.loads((RUNS / EVIDENCE["dev-cheater"] / r["trajectory"]).read_text())
        failed = check("cheater", r, task, traj)
        for attack in plan_attacks(task):
            label = attack.label
            for prefix in ("future commit", "neuter assertions in", "append passing stubs to"):
                if label.startswith(prefix):
                    label = prefix + " …"
            row = rows.setdefault(label, [0, 0])
            row[0] += 1
            row[1] += not any(p.startswith(attack.label) for p in failed)
    for label, (ran, flagged) in rows.items():
        need(f"| {label} |", "attack row")
        line = next((ln for ln in text.splitlines() if ln.startswith(f"| {label} |")), "")
        if not line.endswith(f"| {flagged}/{ran} |"):
            problems.append(f"attack {label!r}: evidence {flagged}/{ran}, README row {line!r}")

    # Baseline smoke test.
    for r in records(EVIDENCE["baseline"]):
        t = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        wall = round((t(r["ended_at"]) - t(r["started_at"])).total_seconds())
        tok = r["tokens"]
        display = r["task_id"].replace("__", "/")  # e.g. PyCQA/isort-2683
        line = next((ln for ln in text.splitlines() if ln.startswith(f"| {display} |")), "")
        if not line:
            problems.append(f"baseline {display}: no README row")
        for value in (
            f"{tok['prompt']:,} / {tok['completion']:,}",
            f"{wall} s",
            f"**{r['reward']}**" if r["reward"] else f"| {r['reward']} ",
        ):
            if value not in line:
                problems.append(f"baseline {display}: {value!r} not in {line!r}")
        if f"| {r['steps']} |" not in line:
            problems.append(f"baseline {r['task_id']}: steps {r['steps']} not in {line!r}")
        for flag in r["flags_raised"]:
            if f"`{flag}`" not in line:
                problems.append(f"baseline {r['task_id']}: flag {flag} not in its README row")

    if dirty != {False}:
        problems.append("some evidence episode ran on a dirty tree")
    for group, found in commits.items():
        if len(found) != 1:
            problems.append(f"{group} evidence not from one commit: {found}")
        else:
            need(f"`{next(iter(found))[:7]}`", f"the {group} evidence commit")

    for p in problems:
        print("FAIL", p)
    oracle_flags = Counter(f for r in oracle_all for f in r["flags_raised"])
    if not problems:
        print(
            f"OK: README Week 2 numbers match data/runs; scripted agents on "
            f"{next(iter(commits['scripted']))[:7]}, baseline on "
            f"{next(iter(commits['baseline']))[:7]}, all clean; "
            f"{sum(len(records(r)) for r in EVIDENCE.values())} "
            f"episodes; all-247 oracle flags: {dict(oracle_flags)}"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
