"""Routing: auto-approve only when nothing argues against it, and fail closed without a
trustworthy calibrator."""

from dataclasses import replace
from decimal import Decimal

import pytest

from manifest_domain.extraction import ExtractedValue
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import DiscrepancyKind, ReviewPriority, ReviewReason
from manifest_domain.routing import ShipmentResults, decide, missing_required

CLEAR = ShipmentResults(
    partial=False,
    discrepancies=(),
    missing_required=False,
    confidences=(Decimal("0.99"), Decimal("0.995")),
    hs_confidences=(Decimal("0.97"),),
    extraction_threshold=Decimal("0.98"),
    hs_threshold=Decimal("0.95"),
)


def test_a_clear_shipment_auto_approves() -> None:
    decision = decide(CLEAR)
    assert decision.auto_approve
    assert decision.reasons == frozenset()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"partial": True}, ReviewReason.PARTIAL_EXTRACTION),
        ({"discrepancies": (DiscrepancyKind.VALUE_MISMATCH,)}, ReviewReason.DISCREPANCY),
        ({"discrepancies": (DiscrepancyKind.MISSING_FIELD,)}, ReviewReason.MISSING_FIELD),
        ({"missing_required": True}, ReviewReason.MISSING_FIELD),
        ({"confidences": (Decimal("0.99"), Decimal("0.97"))}, ReviewReason.LOW_CONFIDENCE),
        ({"confidences": ()}, ReviewReason.LOW_CONFIDENCE),
        ({"extraction_threshold": None}, ReviewReason.LOW_CONFIDENCE),  # no trusted calibrator
        ({"hs_confidences": (Decimal("0.94"),)}, ReviewReason.HS_UNCERTAIN),
        ({"hs_confidences": None}, ReviewReason.HS_UNCERTAIN),  # not classified
        ({"hs_threshold": None}, ReviewReason.HS_UNCERTAIN),
    ],
)
def test_each_reason_blocks_auto_approval(change: dict[str, object], reason: ReviewReason) -> None:
    decision = decide(replace(CLEAR, **change))  # type: ignore[arg-type]
    assert decision.reasons == {reason}
    assert not decision.auto_approve


def test_partial_extractions_and_discrepancies_are_reviewed_first() -> None:
    assert decide(replace(CLEAR, partial=True)).priority is ReviewPriority.HIGH
    discrepancy = replace(CLEAR, discrepancies=(DiscrepancyKind.TOTALS_MISMATCH,))
    assert decide(discrepancy).priority is ReviewPriority.HIGH
    low = replace(CLEAR, confidences=(Decimal("0.5"),))
    assert decide(low).priority is ReviewPriority.NORMAL


def test_missing_required_fields() -> None:
    found = ExtractedValue(value="X", confidence=Decimal("0.9"))
    unread = ExtractedValue(value=None, confidence=Decimal(0))
    fields = {"bl_number": found, "seller_name": unread, "buyer_name": found}
    missing = missing_required(DocumentKind.BILL_OF_LADING, fields)
    assert missing[:2] == ["port_of_discharge", "port_of_loading"]
    assert "seller_name" in missing
    assert "bl_number" not in missing
    assert missing_required(DocumentKind.OTHER, {}) == []
