"""Read the filled-in labelling sheets and report agreement, per classifier model."""

from __future__ import annotations

import csv
import json
from collections import defaultdict

from repogym.calibrate import agreement, precision_recall
from repogym.classify import TaskTypeLabel
from repogym.config import CANDIDATES_PATH, SPEC_SHEET, TASK_TYPE_LABELS_PATH, TASK_TYPE_SHEET
from repogym.models import Candidate
from repogym.tasktype import type_from_labels
from repogym.validate import read_jsonl

TYPE_COLUMN = "my_label (bug / feature / other)"
SPEC_COLUMN = "my_spec_clear (yes / no)"


def _filled(path, column) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            row["task_id"]: row[column].strip().lower()
            for row in csv.DictReader(handle)
            if row.get(column, "").strip()
        }


def report() -> list[str]:
    lines: list[str] = []
    human = _filled(TASK_TYPE_SHEET, TYPE_COLUMN)
    bad = {t: v for t, v in human.items() if v not in ("bug", "feature", "other")}
    if bad:
        lines.append(f"Ignoring {len(bad)} task-type labels not in bug/feature/other: {bad}")
        human = {t: v for t, v in human.items() if t not in bad}
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    github = {
        task_id: kind
        for task_id, c in candidates.items()
        if (kind := type_from_labels(c.pr_labels + c.issue_labels)) != "unknown"
    }
    labels = read_jsonl(TASK_TYPE_LABELS_PATH, TaskTypeLabel)
    by_model: dict[str, dict[str, str]] = defaultdict(dict)
    for label in labels:
        by_model[label.model or "none (failed)"][label.task_id] = label.task_type

    lines.append(f"Task type: {len(human)} human labels, {len(labels)} classifier labels")
    shared = [t for t in human if t in github]
    if shared:
        result = agreement([human[t] for t in shared], [github[t] for t in shared])
        lines.append(f"  GitHub labels vs you:   {result.describe()}")
    for model, predicted in sorted(by_model.items()):
        for name, reference in (("you", human), ("GitHub labels", github)):
            ids = [t for t in reference if t in predicted]
            result = agreement([reference[t] for t in ids], [predicted[t] for t in ids])
            lines.append(f"  {model} vs {name}: {result.describe()}")
            if result.n:
                lines.append(f"    confusion (reference, predicted): {_confusion(result)}")

    spec_human = _filled(SPEC_SHEET, SPEC_COLUMN)
    key_path = SPEC_SHEET.with_suffix(".key.json")
    if spec_human and key_path.exists():
        key = json.loads(key_path.read_text(encoding="utf-8"))
        ids = [t for t in spec_human if t in key and spec_human[t] in ("yes", "no")]
        # Positive class: underspecified (you said "no", or the check flagged it).
        pr = precision_recall(
            [spec_human[t] == "no" for t in ids], [not key[t]["spec_clear"] for t in ids]
        )
        lines.append(
            f"Spec check vs you (n={len(ids)}): precision={_fmt(pr.precision)} "
            f"recall={_fmt(pr.recall)} TP={pr.true_positive} FP={pr.false_positive} "
            f"FN={pr.false_negative} TN={pr.true_negative}"
        )
    else:
        lines.append("Spec sheet not filled in yet")
    return lines


def _confusion(result) -> str:
    return ", ".join(f"{r}->{p}: {n}" for (r, p), n in sorted(result.confusion.items()))


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.2f}"
