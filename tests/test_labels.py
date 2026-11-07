"""Final label precedence and the adjudication file. No network."""

import csv

import pytest

from repogym.calibrate_report import SPEC_COLUMN
from repogym.consensus import Consensus
from repogym.labels import MissingAdjudicationNote, final_label, write_adjudication

APPLIED = Consensus("t", "applied", "feature", ())
DISAGREE = Consensus("t", "disagreement", "unknown", ())


def test_human_beats_github_beats_classifier():
    assert final_label("feature", True, human_type="bug", consensus=APPLIED).task_type_source == (
        "human"
    )
    label = final_label("feature", True, consensus=Consensus("t", "applied", "bug", ()))
    assert (label.task_type, label.task_type_source) == ("feature", "github")
    label = final_label("unknown", True, consensus=APPLIED)
    assert (label.task_type, label.task_type_source) == ("feature", "classifier")


def test_no_consensus_stays_unknown():
    label = final_label("unknown", True, consensus=DISAGREE)
    assert (label.task_type, label.task_type_source) == ("unknown", "unknown")
    assert final_label("unknown", True).task_type_source == "unknown"


def test_spec_human_label_overrides_heuristic_both_ways():
    assert final_label("bug", False, human_spec="yes").spec_clear is True
    assert final_label("bug", True, human_spec="no").spec_clear is False
    label = final_label("bug", False)
    assert (label.spec_clear, label.spec_clear_source) == (False, "heuristic")
    assert final_label("bug", True, human_spec="no").spec_clear_source == "human"


def _sheet(path, notes):
    with path.open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["task_id", SPEC_COLUMN, "notes"])
        for task_id, note in notes.items():
            w.writerow([task_id, "yes", note])


KEY = {"a": {"spec_clear": False, "spec_issues": ["import `X` (t.py:1)"]},
       "b": {"spec_clear": False, "spec_issues": ["keyword `k` (t.py:2)"]}}  # fmt: skip


def test_adjudication_uses_notes_verbatim(tmp_path):
    _sheet(tmp_path / "s.csv", {"a": "Name is an internal test helper, not public API.", "b": "x"})
    write_adjudication(tmp_path / "s.csv", KEY, ["a", "b"], tmp_path / "adj.csv")
    rows = list(csv.DictReader((tmp_path / "adj.csv").open(encoding="utf-8")))
    assert rows[0] == {
        "task_id": "a",
        "blind_spec_clear": "yes",
        "check_flag": "flagged: import `X` (t.py:1)",
        "adjudicated_spec_clear": "yes",
        "adjudication_reason": "Name is an internal test helper, not public API.",
    }


def test_adjudication_refuses_to_invent_a_missing_note(tmp_path):
    _sheet(tmp_path / "s.csv", {"a": "ok", "b": "  "})
    with pytest.raises(MissingAdjudicationNote, match="b"):
        write_adjudication(tmp_path / "s.csv", KEY, ["a", "b"], tmp_path / "adj.csv")
    assert not (tmp_path / "adj.csv").exists()
