"""How a field's confidence is built from its signals (``extraction.Signals``).

1. Page: ``raw = self_report * normaliser factor``; a value only the page's conventions could
   decide keeps ``AMBIGUITY_PENALTY`` of it, one that won't normalise keeps nothing.
2. Document: readings of the same field on several pages are settled (``confidence.settle``).
3. Shipment: readings of the same field on the shipment's other documents that agree, and
   whose own evidence checked out, corroborate it (``agreed``). Disagreement never lowers it:
   when the documents really differ each reading can be right, and the disagreement goes to a
   reviewer as a discrepancy anyway.
4. Calibration maps ``raw`` to a probability (``calibration.Calibrator``). Only then is a
   value whose evidence failed set to 0, so the calibrator can't learn its way around the
   evidence check (``final``).

``SCORE_VERSION`` names this recipe. A calibrator is fitted to one recipe, so changing any
step above means a new version and a refit (``make calibrate``).
"""

from collections.abc import Callable, Sequence
from decimal import Decimal

from manifest_domain.confidence import corroborate, stored
from manifest_domain.extraction import ExtractedValue, Normalised

SCORE_VERSION = 1
AMBIGUITY_PENALTY = Decimal("0.8")
NORMALISER_FACTOR: dict[Normalised, Decimal] = {
    "exact": Decimal(1),
    "ambiguous": AMBIGUITY_PENALTY,
    "failed": Decimal(0),
}


def page_score(self_report: Decimal, normaliser: Normalised) -> Decimal:
    if not 0 <= self_report <= 1:
        raise ValueError(f"self-reported confidence {self_report} is not in [0, 1]")
    return stored(self_report * NORMALISER_FACTOR[normaliser])


def agreed(raw: Decimal, counterparts: Sequence[Decimal]) -> Decimal:
    """``raw`` corroborated by agreeing, evidence-checked readings on other documents."""
    return stored(corroborate(raw, *counterparts)) if counterparts else raw


def final(value: ExtractedValue, calibrate: Callable[[Decimal], Decimal] | None) -> Decimal:
    """The confidence to store and route on: calibrated, and 0 without verified evidence.

    ``calibrate`` None (no calibrator yet) stores the raw score, which is not a probability;
    routing never auto-approves on one (``routing``).
    """
    if value.value is None:
        return Decimal(0)
    if value.signals is not None and value.signals.evidence == "failed":
        return Decimal(0)
    return calibrate(value.confidence) if calibrate is not None else value.confidence
