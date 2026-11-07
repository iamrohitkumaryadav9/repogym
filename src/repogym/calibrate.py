"""Agreement metrics for checking automatic labels against human labels."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Agreement:
    n: int
    accuracy: float | None
    kappa: float | None
    confusion: dict[tuple[str, str], int]

    def describe(self) -> str:
        if not self.n:
            return "n=0"
        kappa = "undefined" if self.kappa is None else f"{self.kappa:.2f}"
        return f"n={self.n} accuracy={self.accuracy:.2f} kappa={kappa}"


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Cohen's kappa for two raters. None when chance agreement is 1 (a single shared label)."""
    if len(a) != len(b):
        raise ValueError("rater sequences differ in length")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    count_a, count_b = Counter(a), Counter(b)
    expected = sum(count_a[k] * count_b[k] for k in set(a) | set(b)) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def agreement(reference: Sequence[str], predicted: Sequence[str]) -> Agreement:
    pairs = list(zip(reference, predicted, strict=True))
    if not pairs:
        return Agreement(0, None, None, {})
    accuracy = sum(r == p for r, p in pairs) / len(pairs)
    return Agreement(len(pairs), accuracy, cohen_kappa(reference, predicted), dict(Counter(pairs)))


@dataclass(frozen=True)
class PrecisionRecall:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def precision(self) -> float | None:
        flagged = self.true_positive + self.false_positive
        return self.true_positive / flagged if flagged else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positive + self.false_negative
        return self.true_positive / actual if actual else None


def precision_recall(actual: Sequence[bool], flagged: Sequence[bool]) -> PrecisionRecall:
    """``actual``/``flagged`` are True for the positive class (here: underspecified)."""
    pairs = list(zip(actual, flagged, strict=True))
    return PrecisionRecall(
        true_positive=sum(a and f for a, f in pairs),
        false_positive=sum(f and not a for a, f in pairs),
        false_negative=sum(a and not f for a, f in pairs),
        true_negative=sum(not a and not f for a, f in pairs),
    )
