"""Test-split protection and pilot hygiene, checked before any episode runs.

* A pilot may touch dev-split tasks only.
* Any run touching test-split tasks is refused unless ALL of these hold:
  1. the experiment references the frozen config (``frozen_config.path``);
  2. that file's sha256 equals the experiment's declared hash AND the hash recorded in
     docs/week3/PREREGISTRATION.md;
  3. the experiment's parameters equal the frozen ones;
  4. the git tree is clean;
  5. ``--final-test-run`` was passed.

Every refusal is appended to data/runs/refusals.jsonl.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from repogym.config import ROOT
from repogym.experiments.spec import Experiment, release_tasks, sha256_bytes

PREREG = ROOT / "docs" / "week3" / "PREREGISTRATION.md"
REFUSALS = ROOT / "data" / "runs" / "refusals.jsonl"
FROZEN_KEYS = (
    "models",
    "strategies",
    "context",
    "budgets",
    "model_settings",
    "limits_margin",
    "prompts",
    "tools",
)


class RunRefused(RuntimeError):
    pass


def git_clean(root: Path = ROOT) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    return not out.strip()


def refuse(experiment: Experiment, reason: str, refusals: Path) -> None:
    refusals.parent.mkdir(parents=True, exist_ok=True)
    with refusals.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "experiment": experiment.name,
                    "kind": experiment.kind,
                    "file_sha256": experiment.file_sha256,
                    "reason": reason,
                }
            )
            + "\n"
        )
    raise RunRefused(reason)


def check(
    experiment: Experiment,
    final_test_run: bool = False,
    *,
    prereg: Path = PREREG,
    refusals: Path = REFUSALS,
    clean=git_clean,
    root: Path = ROOT,
) -> None:
    tasks = release_tasks(experiment.release)
    splits = {tasks[t].split for t in experiment.task_ids}
    if experiment.kind == "pilot" and splits != {"dev"}:
        refuse(
            experiment, f"a pilot may use dev tasks only (got splits {sorted(splits)})", refusals
        )
    if "test" not in splits:
        if final_test_run:
            refuse(experiment, "--final-test-run given, but the run touches no test task", refusals)
        return
    frozen = experiment.frozen_config or {}
    if not frozen.get("path") or not frozen.get("sha256"):
        refuse(experiment, "test-split run without a frozen_config (path and sha256)", refusals)
    path = root / frozen["path"]
    if not path.exists():
        refuse(experiment, f"frozen config {frozen['path']} does not exist", refusals)
    actual = sha256_bytes(path.read_bytes())
    if actual != frozen["sha256"]:
        refuse(
            experiment,
            f"frozen config hash mismatch: file {actual[:12]}, declared {frozen['sha256'][:12]}",
            refusals,
        )
    if not prereg.exists() or actual not in prereg.read_text(encoding="utf-8"):
        refuse(experiment, "frozen config hash is not recorded in PREREGISTRATION.md", refusals)
    frozen_values = yaml.safe_load(path.read_text(encoding="utf-8"))
    resolved = experiment.resolved()
    differing = [k for k in FROZEN_KEYS if frozen_values.get(k) != resolved.get(k)]
    if differing:
        refuse(experiment, f"experiment differs from the frozen config in {differing}", refusals)
    if not clean():
        refuse(experiment, "the git tree is not clean", refusals)
    if not final_test_run:
        refuse(experiment, "test-split run without --final-test-run", refusals)
