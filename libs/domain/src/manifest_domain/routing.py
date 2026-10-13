"""Review or auto-approve: the rules that turn a shipment's results into a decision.

A shipment is auto-approved only when nothing argues against it:

- every page of every document was extracted (else ``PARTIAL_EXTRACTION``);
- no unresolved cross-document discrepancy (``DISCREPANCY``), and nothing required is missing,
  neither a header field (``REQUIRED``) nor a line's origin (``MISSING_FIELD``);
- the least confident extracted value reaches the extraction calibrator's threshold
  (``LOW_CONFIDENCE``): ``weakest``, not ``joint``, which across ~60 values would approve
  nothing;
- every line's HS suggestion reaches the HS calibrator's threshold (``HS_UNCERTAIN``).

Thresholds come with calibrators fitted on a real model's answers. With no such calibrator (none
yet, fitted to another scoring recipe, or fitted on the ground-truth oracle) the threshold is
None and the rule fails closed: nothing auto-approves on confidences that aren't probabilities.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from manifest_domain.confidence import weakest
from manifest_domain.extraction import ExtractedValue
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import DiscrepancyKind, ReviewPriority, ReviewReason

REQUIRED: dict[DocumentKind, frozenset[str]] = {
    DocumentKind.COMMERCIAL_INVOICE: frozenset(
        {
            "invoice_number",
            "invoice_date",
            "seller_name",
            "buyer_name",
            "currency",
            "incoterm",
            "total_amount",
        }
    ),
    DocumentKind.PACKING_LIST: frozenset(
        {"invoice_number", "total_packages", "total_gross_weight_kg"}
    ),
    DocumentKind.BILL_OF_LADING: frozenset(
        {
            "bl_number",
            "seller_name",
            "buyer_name",
            "port_of_loading",
            "port_of_discharge",
            "total_packages",
            "total_gross_weight_kg",
        }
    ),
}
_URGENT_REASONS = frozenset({ReviewReason.PARTIAL_EXTRACTION, ReviewReason.DISCREPANCY})


def missing_required(kind: DocumentKind, fields: Mapping[str, ExtractedValue]) -> list[str]:
    """Required fields of a ``kind`` document that weren't found."""
    return sorted(
        name
        for name in REQUIRED.get(kind, frozenset())
        if name not in fields or fields[name].value is None
    )


@dataclass(frozen=True)
class ShipmentResults:
    partial: bool
    """A document failed or has pages that failed."""
    discrepancies: Sequence[DiscrepancyKind]
    """Unresolved ones."""
    missing_required: bool
    confidences: Sequence[Decimal]
    """Final confidence of every extracted value, header and line, on every document."""
    hs_confidences: Sequence[Decimal] | None
    """One per classified line (0 for a line without a suggestion); None if not classified."""
    extraction_threshold: Decimal | None
    hs_threshold: Decimal | None


@dataclass(frozen=True)
class Decision:
    reasons: frozenset[ReviewReason]
    priority: ReviewPriority

    @property
    def auto_approve(self) -> bool:
        return not self.reasons


def _below(confidences: Iterable[Decimal], threshold: Decimal | None) -> bool:
    values = list(confidences)
    return threshold is None or (bool(values) and weakest(values) < threshold)


def decide(results: ShipmentResults) -> Decision:
    reasons: set[ReviewReason] = set()
    if results.partial:
        reasons.add(ReviewReason.PARTIAL_EXTRACTION)
    if any(kind != DiscrepancyKind.MISSING_FIELD for kind in results.discrepancies):
        reasons.add(ReviewReason.DISCREPANCY)
    if results.missing_required or DiscrepancyKind.MISSING_FIELD in results.discrepancies:
        reasons.add(ReviewReason.MISSING_FIELD)
    if not results.confidences or _below(results.confidences, results.extraction_threshold):
        reasons.add(ReviewReason.LOW_CONFIDENCE)
    if results.hs_confidences is None or _below(results.hs_confidences, results.hs_threshold):
        reasons.add(ReviewReason.HS_UNCERTAIN)
    urgent = reasons & _URGENT_REASONS
    priority = ReviewPriority.HIGH if urgent else ReviewPriority.NORMAL
    return Decision(frozenset(reasons), priority)
