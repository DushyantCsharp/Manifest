"""The numbers the eval report is made of: accuracy, F1, calibration error, precision/coverage.

Pure functions over Decimals, so every figure in a report can be recomputed by hand.

- **ECE** (expected calibration error): confidences are split into ``BINS`` equal-width bins;
  each bin contributes the gap between its mean confidence and its accuracy, weighted by its
  share of the samples. 0 is perfect calibration.
- **Precision/coverage**: auto-approving every value whose confidence reaches a threshold
  covers some share of the values (coverage) at some accuracy among them (precision). The
  auto-approve threshold is the lowest one whose precision reaches the target on the
  calibration split, which maximises coverage there; the report shows what it does on the
  test split.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

BINS = 10
ZERO, ONE = Decimal(0), Decimal(1)


@dataclass(frozen=True)
class Scored:
    """One prediction: how confident, and whether it was right."""

    confidence: Decimal
    correct: bool


@dataclass(frozen=True)
class Bin:
    low: Decimal
    high: Decimal
    count: int
    confidence: Decimal | None  # mean confidence of its samples
    accuracy: Decimal | None


def ratio(part: int, whole: int) -> Decimal | None:
    return Decimal(part) / Decimal(whole) if whole else None


def reliability(samples: Sequence[Scored], bins: int = BINS) -> list[Bin]:
    width = ONE / bins
    grouped: list[list[Scored]] = [[] for _ in range(bins)]
    for sample in samples:
        index = min(int(sample.confidence / width), bins - 1)  # 1.0 belongs to the last bin
        grouped[index].append(sample)
    result = []
    for index, members in enumerate(grouped):
        mean = sum((s.confidence for s in members), ZERO) / len(members) if members else None
        right = sum(1 for s in members if s.correct)
        accuracy = ratio(right, len(members))
        result.append(Bin(width * index, width * (index + 1), len(members), mean, accuracy))
    return result


def ece(samples: Sequence[Scored], bins: int = BINS) -> Decimal | None:
    if not samples:
        return None
    total = Decimal(len(samples))
    gaps = (
        Decimal(b.count) / total * abs(b.confidence - b.accuracy)
        for b in reliability(samples, bins)
        if b.confidence is not None and b.accuracy is not None
    )
    return sum(gaps, ZERO)


@dataclass(frozen=True)
class Point:
    threshold: Decimal
    coverage: Decimal
    precision: Decimal
    count: int


def curve(samples: Sequence[Scored]) -> list[Point]:
    """Precision and coverage of auto-approving at each distinct confidence, highest first."""
    total = len(samples)
    points = []
    for threshold in sorted({s.confidence for s in samples}, reverse=True):
        kept = [s for s in samples if s.confidence >= threshold]
        right = sum(1 for s in kept if s.correct)
        points.append(
            Point(threshold, Decimal(len(kept)) / total, Decimal(right) / len(kept), len(kept))
        )
    return points


def choose_threshold(samples: Sequence[Scored], target: Decimal) -> Decimal | None:
    """The lowest positive confidence whose precision reaches ``target`` (None: none does)."""
    chosen = None
    for point in curve(samples):
        if point.precision >= target and point.threshold > ZERO:
            chosen = point.threshold
    return chosen


def at(samples: Sequence[Scored], threshold: Decimal | None) -> tuple[Decimal, Decimal | None]:
    """(coverage, precision) of auto-approving at ``threshold`` (None: nothing)."""
    if threshold is None or not samples:
        return ZERO, None
    kept = [s for s in samples if s.confidence >= threshold]
    right = sum(1 for s in kept if s.correct)
    return Decimal(len(kept)) / len(samples), ratio(right, len(kept))


def percentile(values: Sequence[int], q: int) -> int | None:
    """Nearest-rank percentile (``q`` in 1..100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (Decimal(q) / 100 * len(ordered)).to_integral_value(ROUND_CEILING)
    return ordered[max(int(rank), 1) - 1]


def f1(true_positive: int, predicted: int, actual: int) -> tuple[Decimal | None, ...]:
    """(precision, recall, F1)."""
    precision, recall = ratio(true_positive, predicted), ratio(true_positive, actual)
    if precision is None or recall is None:
        return precision, recall, None
    if precision + recall == 0:
        return precision, recall, ZERO
    return precision, recall, 2 * precision * recall / (precision + recall)
