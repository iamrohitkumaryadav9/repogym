"""Read the filled-in labelling sheets and report agreement, per classifier model."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict

from repogym.calibrate import agreement, precision_recall
from repogym.classify import TaskTypeLabel
from repogym.config import CANDIDATES_PATH, SPEC_SHEET, TASK_TYPE_LABELS_PATH, TASK_TYPE_SHEET
from repogym.models import Candidate
from repogym.tasktype import type_from_labels
from repogym.validate import read_jsonl

TYPE_COLUMN = "my_label (bug / feature / other)"
SPEC_COLUMN = "my_spec_clear (yes / no)"


def _read_sheet(path, column) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["task_id"]: row for row in csv.DictReader(handle)}


def _filled(path, column) -> dict[str, str]:
    return {
        task_id: row[column].strip().lower()
        for task_id, row in _read_sheet(path, column).items()
        if (row.get(column) or "").strip()
    }


def _invalid(sheet: dict[str, str], allowed: tuple[str, ...], name: str) -> list[str]:
    bad = {t: v for t, v in sheet.items() if v not in allowed}
    if not bad:
        return [f"  {name}: every label is one of {'/'.join(allowed)}"]
    return [f"  {name}: NOT an allowed value (ignored, not guessed): {t} = {v!r}"
            for t, v in sorted(bad.items())]  # fmt: skip


def report() -> list[str]:
    lines: list[str] = []
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    type_rows = _read_sheet(TASK_TYPE_SHEET, TYPE_COLUMN)
    human = _filled(TASK_TYPE_SHEET, TYPE_COLUMN)
    spec_human = _filled(SPEC_SHEET, SPEC_COLUMN)
    lines.append("Sheet values")
    lines += _invalid(human, ("bug", "feature", "other"), "task-type sheet")
    lines += _invalid(spec_human, ("yes", "no"), "spec sheet")
    blank = [t for t in type_rows if t not in human]
    if blank:
        lines.append(f"  task-type sheet: {len(blank)} rows left blank: {sorted(blank)}")
    human = {t: v for t, v in human.items() if v in ("bug", "feature", "other")}
    spec_human = {t: v for t, v in spec_human.items() if v in ("yes", "no")}

    github = {
        task_id: kind
        for task_id, c in candidates.items()
        if (kind := type_from_labels(c.pr_labels + c.issue_labels)) != "unknown"
    }
    labels = {label.task_id: label for label in read_jsonl(TASK_TYPE_LABELS_PATH, TaskTypeLabel)}
    by_model: dict[str, dict[str, str]] = defaultdict(dict)
    for label in labels.values():
        by_model[label.model or "none (failed)"][label.task_id] = label.task_type

    lines.append("")
    lines.append(f"Task type: {len(human)} human labels, {len(labels)} classifier labels")
    counts = Counter(human.values())
    lines.append(
        "  your labels: " + ", ".join(f"{k}={counts[k]}" for k in ("bug", "feature", "other"))
    )
    lines.append(f"  your 'other' labels: {counts['other']} (the classifier has predicted 'other' "
                 f"{sum(lb.task_type == 'other' for lb in labels.values())} times)")  # fmt: skip
    failed = sorted(t for t, lb in labels.items() if lb.task_type == "unknown")
    lines.append(f"  classifier failures (unknown): {len(failed)} {failed if failed else ''}")
    shared = [t for t in human if t in github]
    if shared:
        result = agreement([human[t] for t in shared], [github[t] for t in shared])
        lines.append(f"  GitHub labels vs you:   {result.describe()}")
    for model, predicted in sorted(by_model.items()):
        for name, reference in (("you", human), ("GitHub labels", github)):
            ids = sorted(t for t in reference if t in predicted)
            result = agreement([reference[t] for t in ids], [predicted[t] for t in ids])
            lines.append(f"  {model} vs {name}: {result.describe()}")
            if result.n:
                lines.append(f"    confusion (reference -> predicted): {_confusion(result)}")
            wrong = [t for t in ids if reference[t] != predicted[t]]
            for t in wrong:
                lb = labels[t]
                title = candidates[t].issue_title if t in candidates else ""
                lines.append(f"    - {t}: {name}={reference[t]} classifier={predicted[t]} "
                             f"({lb.confidence}) | {title}")  # fmt: skip
                lines.append(f"        rationale: {lb.rationale or lb.error}")

    lines.append("")
    key_path = SPEC_SHEET.with_suffix(".key.json")
    if spec_human and key_path.exists():
        key = json.loads(key_path.read_text(encoding="utf-8"))
        spec_rows = _read_sheet(SPEC_SHEET, SPEC_COLUMN)
        ids = sorted(t for t in spec_human if t in key)
        # Positive class: underspecified (you said "no", or the check flagged it).
        pr = precision_recall(
            [spec_human[t] == "no" for t in ids], [not key[t]["spec_clear"] for t in ids]
        )
        lines.append(
            f"Spec check vs you (n={len(ids)}, positive = underspecified): "
            f"precision={_fmt(pr.precision)} recall={_fmt(pr.recall)} f1={_fmt(pr.f1)} "
            f"TP={pr.true_positive} FP={pr.false_positive} "
            f"FN={pr.false_negative} TN={pr.true_negative}"
        )
        for t in ids:
            flagged, unclear = not key[t]["spec_clear"], spec_human[t] == "no"
            if flagged == unclear:
                continue
            kind = (
                "FP (flagged, you said clear)" if flagged else "FN (not flagged, you said unclear)"
            )
            lines.append(
                f"  - {kind}: {t} | {candidates[t].issue_title if t in candidates else ''}"
            )
            if flagged:
                lines.append(f"      check flagged: {'; '.join(key[t]['spec_issues'])}")
            note = (spec_rows.get(t, {}).get("notes") or "").strip()
            if note:
                lines.append(f"      your note: {note}")
    else:
        lines.append("Spec sheet not filled in yet")
    return lines


def _confusion(result) -> str:
    return ", ".join(f"{r}->{p}: {n}" for (r, p), n in sorted(result.confusion.items()))


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.2f}"
