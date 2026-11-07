"""Final task_type and spec_clear for a release, each with the source it came from.

task_type precedence: human (labelling sheet) > github (PR/issue labels) > classifier (three-run
consensus) > unknown. spec_clear: human label where there is one, otherwise the heuristic check.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from repogym.calibrate_report import SPEC_COLUMN, TYPE_COLUMN, _read_sheet
from repogym.consensus import Consensus

TaskTypeSource = str  # "human" | "github" | "classifier" | "unknown"


@dataclass(frozen=True)
class FinalLabel:
    task_type: str
    task_type_source: TaskTypeSource
    spec_clear: bool
    spec_clear_source: str  # "human" | "heuristic"


def final_label(
    github_type: str,
    heuristic_spec_clear: bool,
    human_type: str | None = None,
    human_spec: str | None = None,
    consensus: Consensus | None = None,
) -> FinalLabel:
    if human_type in ("bug", "feature", "other"):
        task_type, source = human_type, "human"
    elif github_type in ("bug", "feature"):
        task_type, source = github_type, "github"
    elif consensus is not None and consensus.outcome == "applied":
        task_type, source = consensus.task_type, "classifier"
    else:
        task_type, source = "unknown", "unknown"
    if human_spec in ("yes", "no"):
        spec, spec_source = human_spec == "yes", "human"
    else:
        spec, spec_source = heuristic_spec_clear, "heuristic"
    return FinalLabel(task_type, source, spec, spec_source)


def human_labels(type_sheet: Path, spec_sheet: Path) -> tuple[dict[str, str], dict[str, str]]:
    def column(path: Path, name: str, allowed: tuple[str, ...]) -> dict[str, str]:
        rows = _read_sheet(path, name)
        values = {t: (r.get(name) or "").strip().lower() for t, r in rows.items()}
        return {t: v for t, v in values.items() if v in allowed}

    return (
        column(type_sheet, TYPE_COLUMN, ("bug", "feature", "other")),
        column(spec_sheet, SPEC_COLUMN, ("yes", "no")),
    )


class MissingAdjudicationNote(ValueError):
    pass


ADJUDICATION_COLUMNS = [
    "task_id",
    "blind_spec_clear",
    "check_flag",
    "adjudicated_spec_clear",
    "adjudication_reason",
]


def write_adjudication(spec_sheet: Path, key: dict, task_ids: list[str], out: Path) -> None:
    """Record upheld blind labels with the labeller's own notes, verbatim. Never invents one."""
    rows = _read_sheet(spec_sheet, SPEC_COLUMN)
    missing = [t for t in task_ids if not (rows.get(t, {}).get("notes") or "").strip()]
    if missing:
        raise MissingAdjudicationNote(f"no note in {spec_sheet.name} for: {', '.join(missing)}")
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(ADJUDICATION_COLUMNS)
        for t in task_ids:
            blind = rows[t][SPEC_COLUMN].strip().lower()
            flag = (
                "flagged: " + "; ".join(key[t]["spec_issues"]) if not key[t]["spec_clear"] else ""
            )
            writer.writerow([t, blind, flag or "clear", blind, rows[t]["notes"]])
