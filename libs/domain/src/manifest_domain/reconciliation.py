"""Cross-document reconciliation: where a shipment's invoice, packing list and bill of lading
disagree, and which values another document corroborates.

Pure: it takes each document's merged extraction and returns ``findings`` (the discrepancies
to record and route on) and, for every value it compared, whether the other documents agree
(``agreement``, a confidence signal) with the confidences of those that do (``support``).

- Header fields several documents state must be equal: the invoice number, seller and buyer,
  the Incoterm and its place, the package count and the gross weight. Names compare ignoring
  case, spacing and punctuation, numbers numerically, and weights within ``WEIGHT_TOLERANCE``
  (a layout printing pounds rounds to 0.1 lb).
- The invoice total must equal the sum of its line amounts.
- Invoice and packing-list lines are paired (``alignment``). A line on one document only is a
  line-items mismatch; a paired line's quantity and origin must agree; an origin missing on
  either side is a missing field (stays empty, is never filled in).

Nothing else is compared where a value wasn't read: a missing value is a confidence problem
and is routed as one, not evidence that the documents disagree.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from math import prod
from typing import Literal

from manifest_domain.alignment import align, line_distance
from manifest_domain.extraction import Agreement, ExtractedValue
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import DiscrepancyKind, DiscrepancySeverity
from manifest_domain.scoring import agreed

CI = DocumentKind.COMMERCIAL_INVOICE
PL = DocumentKind.PACKING_LIST
BL = DocumentKind.BILL_OF_LADING
WEIGHT_TOLERANCE = Decimal("0.005")  # relative

Comparison = Literal["exact", "name", "number", "weight"]
Ref = tuple[DocumentKind, str, int | None]
"""A compared value: document, field, and 1-based line number (None for header fields)."""

HEADER_CHECKS: tuple[tuple[str, Comparison, tuple[DocumentKind, ...]], ...] = (
    ("invoice_number", "exact", (CI, PL)),
    ("seller_name", "name", (CI, PL, BL)),
    ("buyer_name", "name", (CI, PL, BL)),
    ("incoterm", "exact", (CI, PL)),
    ("incoterm_place", "name", (CI, PL)),
    ("total_packages", "number", (PL, BL)),
    ("total_gross_weight_kg", "weight", (PL, BL)),
)
# (field, comparison, report a disagreement): descriptions are paired but often worded
# differently on purpose, so they only corroborate.
LINE_CHECKS: tuple[tuple[str, Comparison, bool], ...] = (
    ("description", "name", False),
    ("quantity", "number", True),
    ("country_of_origin", "exact", True),
)
SEVERITY = {
    DiscrepancyKind.VALUE_MISMATCH: DiscrepancySeverity.BLOCKING,
    DiscrepancyKind.TOTALS_MISMATCH: DiscrepancySeverity.BLOCKING,
    DiscrepancyKind.LINE_ITEMS_MISMATCH: DiscrepancySeverity.BLOCKING,
    DiscrepancyKind.MISSING_FIELD: DiscrepancySeverity.WARNING,
}


@dataclass(frozen=True)
class DocumentReading:
    """One document's merged extraction."""

    kind: DocumentKind
    fields: Mapping[str, ExtractedValue]
    lines: Sequence[Mapping[str, ExtractedValue]] = ()


@dataclass(frozen=True)
class Observation:
    document: DocumentKind
    value: str | None
    page: int | None = None
    line_no: int | None = None


@dataclass(frozen=True)
class Finding:
    kind: DiscrepancyKind
    field_key: str
    line_no: int | None
    message: str
    observations: tuple[Observation, ...]

    @property
    def severity(self) -> DiscrepancySeverity:
        return SEVERITY[self.kind]


@dataclass
class Reconciliation:
    findings: list[Finding] = field(default_factory=list)
    agreement: dict[Ref, Agreement] = field(default_factory=dict)
    support: dict[Ref, list[Decimal]] = field(default_factory=dict)


def _name(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


def same(a: str, b: str, comparison: Comparison) -> bool:
    if comparison == "name":
        return _name(a) == _name(b)
    if comparison in ("number", "weight"):
        try:
            x, y = Decimal(a), Decimal(b)
        except InvalidOperation:
            return a == b
        if comparison == "number":
            return x == y
        return abs(x - y) <= WEIGHT_TOLERANCE * max(abs(x), abs(y))
    return a == b


def _verified(value: ExtractedValue) -> bool:
    return value.signals is None or value.signals.evidence == "verified"


def _observe(kind: DocumentKind, value: ExtractedValue | None, line_no: int | None) -> Observation:
    if value is None:
        return Observation(kind, None, None, line_no)
    return Observation(kind, value.value, value.page, line_no)


def _describe(name: str, observations: Sequence[Observation]) -> str:
    stated = ", ".join(f"{o.document.value} {o.value!r}" for o in observations)
    return f"{name} differs: {stated}"


class _Comparer:
    def __init__(self) -> None:
        self.result = Reconciliation()

    def compare(
        self, comparison: Comparison, readings: Sequence[tuple[Ref, ExtractedValue]]
    ) -> bool:
        """Record agreement for each read value against the others; True if all agree."""
        present = [(ref, value.value, value) for ref, value in readings if value.value is not None]
        all_agree = True
        for ref, text, _ in present:
            others = [(other, v) for r, other, v in present if r != ref]
            if not others:
                self.result.agreement[ref] = "single"
                continue
            agreeing = [v for other, v in others if same(text, other, comparison)]
            agree = len(agreeing) == len(others)
            self.result.agreement[ref] = "agree" if agree else "disagree"
            self.result.support[ref] = [v.confidence for v in agreeing if _verified(v)]
            all_agree &= agree
        return all_agree

    def find(
        self,
        kind: DiscrepancyKind,
        field_key: str,
        line_no: int | None,
        message: str,
        observations: Sequence[Observation],
    ) -> None:
        self.result.findings.append(Finding(kind, field_key, line_no, message, tuple(observations)))


def _headers(comparer: _Comparer, documents: Mapping[DocumentKind, DocumentReading]) -> None:
    for name, comparison, kinds in HEADER_CHECKS:
        readings: list[tuple[Ref, ExtractedValue]] = [
            ((kind, name, None), documents[kind].fields[name])
            for kind in kinds
            if kind in documents and name in documents[kind].fields
        ]
        if not comparer.compare(comparison, readings):
            observations = [_observe(ref[0], value, None) for ref, value in readings if value.value]
            message = _describe(name, observations)
            comparer.find(DiscrepancyKind.VALUE_MISMATCH, name, None, message, observations)


def _invoice_total(comparer: _Comparer, invoice: DocumentReading) -> None:
    total = invoice.fields.get("total_amount")
    amounts = [line.get("amount") for line in invoice.lines]
    read = [a for a in amounts if a is not None and a.value is not None]
    if total is None or total.value is None or not amounts or len(read) < len(amounts):
        return  # nothing stated, or an unread amount: nothing to add up
    summed = sum((Decimal(str(a.value)) for a in read), Decimal(0))
    ref: Ref = (CI, "total_amount", None)
    if Decimal(total.value) == summed:
        comparer.result.agreement[ref] = "agree"
        if all(_verified(a) for a in read):
            comparer.result.support[ref] = [prod((a.confidence for a in read), start=Decimal(1))]
        return
    comparer.result.agreement[ref] = "disagree"
    message = f"total_amount {total.value} but the lines add up to {summed}"
    observations = [_observe(CI, total, None)]
    comparer.find(DiscrepancyKind.TOTALS_MISMATCH, "total_amount", None, message, observations)


def _values(line: Mapping[str, ExtractedValue]) -> dict[str, str | None]:
    return {name: value.value for name, value in line.items()}


def _unstated(value: ExtractedValue | None) -> bool:
    return value is None or value.value is None


def _lines(comparer: _Comparer, invoice: DocumentReading, packing: DocumentReading) -> None:
    left, right = list(invoice.lines), list(packing.lines)
    if not left or not right:
        return
    alignment = align([_values(x) for x in left], [_values(x) for x in right], line_distance)
    unpaired = DiscrepancyKind.LINE_ITEMS_MISMATCH
    for i in alignment.left_only:
        observation = _observe(CI, left[i].get("description"), i + 1)
        message = f"invoice line {i + 1} is not on the packing list"
        comparer.find(unpaired, "line_items", i + 1, message, [observation])
    for j in alignment.right_only:
        observation = _observe(PL, right[j].get("description"), j + 1)
        message = f"packing list line {j + 1} is not on the invoice"
        comparer.find(unpaired, "packing_list.line_items", j + 1, message, [observation])
    for i, j in alignment.pairs:
        for name, comparison, report in LINE_CHECKS:
            a, b = left[i].get(name), right[j].get(name)
            key = f"line_items.{name}"
            observations = [_observe(CI, a, i + 1), _observe(PL, b, j + 1)]
            if name == "country_of_origin" and (_unstated(a) or _unstated(b)):
                message = f"invoice line {i + 1}: origin not stated on every document"
                comparer.find(DiscrepancyKind.MISSING_FIELD, key, i + 1, message, observations)
                continue
            if a is None or b is None:
                continue
            readings: list[tuple[Ref, ExtractedValue]] = [
                ((CI, name, i + 1), a),
                ((PL, name, j + 1), b),
            ]
            if not comparer.compare(comparison, readings) and report:
                message = f"invoice line {i + 1}: " + _describe(name, observations)
                comparer.find(DiscrepancyKind.VALUE_MISMATCH, key, i + 1, message, observations)


def reconcile(documents: Sequence[DocumentReading]) -> Reconciliation:
    """Compare a shipment's documents (the first of each kind; others aren't compared)."""
    by_kind: dict[DocumentKind, DocumentReading] = {}
    for document in documents:
        by_kind.setdefault(document.kind, document)
    comparer = _Comparer()
    _headers(comparer, by_kind)
    if CI in by_kind:
        _invoice_total(comparer, by_kind[CI])
    if CI in by_kind and PL in by_kind:
        _lines(comparer, by_kind[CI], by_kind[PL])
    return comparer.result


def corroborated(reading: DocumentReading, result: Reconciliation) -> DocumentReading:
    """``reading`` with every value's agreement signal set, and its confidence raised by the
    documents that agree with it (``scoring.agreed``; disagreement lowers nothing)."""

    def update(value: ExtractedValue, ref: Ref) -> ExtractedValue:
        signals = value.signals
        if signals is not None:
            signals = signals.model_copy(update={"agreement": result.agreement.get(ref, "single")})
        confidence = value.confidence
        if value.value is not None:
            confidence = agreed(confidence, result.support.get(ref, []))
        return value.model_copy(update={"confidence": confidence, "signals": signals})

    kind = reading.kind
    fields = {name: update(value, (kind, name, None)) for name, value in reading.fields.items()}
    lines = [
        {name: update(value, (kind, name, number)) for name, value in line.items()}
        for number, line in enumerate(reading.lines, start=1)
    ]
    return DocumentReading(kind, fields, lines)
