"""Classify a task as a bug fix or a feature from its PR and issue labels."""

from __future__ import annotations

import re
from collections.abc import Callable

from repogym.models import Candidate, TaskType

_BUG = re.compile(r"\b(bug|crash|regression|defect|error)\b", re.IGNORECASE)
_FEATURE = re.compile(r"\b(feature|enhancement|proposal|new api)\b", re.IGNORECASE)

# Hook for a classifier used when labels are inconclusive; returning None keeps "unknown".
# TODO(week2): plug in an LLM classifier that reads the issue and PR text.
TaskTypeClassifier = Callable[[Candidate], TaskType | None]


def type_from_labels(labels: list[str]) -> TaskType:
    """Labels that point one way decide; none or conflicting ones give "unknown"."""
    is_bug = any(_BUG.search(label) for label in labels)
    is_feature = any(_FEATURE.search(label) for label in labels)
    if is_bug and not is_feature:
        return "bug"
    if is_feature and not is_bug:
        return "feature"
    return "unknown"


def classify_task_type(
    candidate: Candidate, classifier: TaskTypeClassifier | None = None
) -> TaskType:
    task_type = type_from_labels(candidate.pr_labels + candidate.issue_labels)
    if task_type == "unknown" and classifier is not None:
        return classifier(candidate) or "unknown"
    return task_type
