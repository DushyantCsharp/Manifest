"""Combining confidences as values flow from readings to fields to documents.

A confidence is the probability that a value is correct, and these rules keep it one:

- ``corroborate``: independent readings that agree. Each reading is evidence with likelihood
  ratio p / (1 - p) against an even prior, so log-odds add: agreement raises confidence and
  0.5 changes nothing. It is the conservative case where all wrong readings coincide.
- ``contradict``: a reading disputed by an independent rival reading (only ever lowers it).
- ``settle``: one value for a field read several times (say, on every page).
- ``joint``: the probability that every one of several values is right, which is what an
  auto-approve decision needs. ``weakest`` is the tightest bound any single value gives.

No reading is ever certain: inputs and results are kept within [FLOOR, CEILING] (the precision
confidences are stored at), so one overconfident reading can't outvote every other and
certainty on both sides of a contradiction can't produce 0/0.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import ROUND_FLOOR, Decimal
from math import prod

from manifest_domain.extraction import ExtractedValue

STORED = Decimal("0.0001")  # numeric(5,4)
FLOOR = STORED
CEILING = 1 - STORED


def _bound(confidence: Decimal) -> Decimal:
    return min(max(confidence, FLOOR), CEILING)


def _clamp(confidence: Decimal) -> Decimal:
    if not 0 <= confidence <= 1:
        raise ValueError(f"confidence {confidence} is not in [0, 1]")
    return _bound(confidence)


def stored(confidence: Decimal) -> Decimal:
    """Rounded down to storage precision, so a stored confidence never overstates."""
    return confidence.quantize(STORED, ROUND_FLOOR)


def corroborate(*confidences: Decimal) -> Decimal:
    """Confidence in a value that independent readings, each this confident, all agree on."""
    if not confidences:
        raise ValueError("nothing to corroborate")
    clamped = [_clamp(c) for c in confidences]
    right = prod(clamped, start=Decimal(1))
    wrong = prod((1 - c for c in clamped), start=Decimal(1))
    return _bound(right / (right + wrong))


def contradict(confidence: Decimal, rival: Decimal) -> Decimal:
    """Confidence in a reading after an independent reading this confident disagreed.

    Disagreement only ever lowers confidence. A rival below 0.5 is no evidence either way:
    there are many ways to be wrong, so a doubtful rival says nothing about this reading.
    """
    return corroborate(confidence, 1 - max(_clamp(rival), Decimal("0.5")))


def joint(confidences: Iterable[Decimal]) -> Decimal:
    """Probability that every value is right, if their errors are independent."""
    return prod((_clamp(c) for c in confidences), start=Decimal(1))


def weakest(confidences: Iterable[Decimal]) -> Decimal:
    return min((_clamp(c) for c in confidences), default=Decimal(1))


def _verified(reading: ExtractedValue) -> bool:
    """Evidence checked out (readings from before schema v3 carry no evidence to check)."""
    return reading.signals is None or reading.signals.evidence == "verified"


def settle(readings: Sequence[ExtractedValue]) -> ExtractedValue:
    """One value for a field read several times: the best-supported value, weighed against
    the best-supported rival. A reading that found nothing only counts if nothing was found,
    and one whose evidence failed only if no reading's evidence checked out. The result keeps
    the provenance (page, evidence, signals) of the strongest reading of the winning value.
    """
    if not readings:
        raise ValueError("no readings to settle")
    found = [r for r in readings if r.value is not None] or list(readings)
    candidates = [r for r in found if _verified(r)] or found
    by_value: dict[str | None, list[ExtractedValue]] = defaultdict(list)
    for reading in candidates:
        by_value[reading.value].append(reading)
    support = {value: corroborate(*(r.confidence for r in rs)) for value, rs in by_value.items()}
    ranked = sorted(support.items(), key=lambda item: item[1], reverse=True)
    (value, confidence), rivals = ranked[0], ranked[1:]
    if rivals:
        confidence = contradict(confidence, rivals[0][1])
    strongest = max(by_value[value], key=lambda reading: reading.confidence)
    return strongest.model_copy(update={"confidence": stored(confidence)})
