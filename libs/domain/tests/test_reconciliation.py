"""Cross-document reconciliation: every kind of deliberate mismatch the synthetic packs inject
is found, a consistent shipment raises nothing, and agreement only ever corroborates."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from manifest_domain.extraction import Evidence, ExtractedValue, Signals
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.reconciliation import (
    DocumentReading,
    Reconciliation,
    corroborated,
    reconcile,
    same,
)
from manifest_domain.review import DiscrepancyKind, DiscrepancySeverity

CI = DocumentKind.COMMERCIAL_INVOICE
PL = DocumentKind.PACKING_LIST
BL = DocumentKind.BILL_OF_LADING
VALUE = DiscrepancyKind.VALUE_MISMATCH
Doc = dict[str, Any]


def v(text: str | None, confidence: str = "0.9", evidence: Evidence = "verified") -> ExtractedValue:
    signals = Signals(self_report=Decimal(confidence), evidence=evidence, normaliser="exact")
    return ExtractedValue(
        value=text, confidence=Decimal(confidence), page=1, evidence=text, signals=signals
    )


def shipment() -> dict[DocumentKind, Doc]:
    """A consistent shipment as plain values (mutated per test, then read)."""
    lines = [
        {"description": "Laptop computer 14in", "quantity": "40", "country_of_origin": "CN"},
        {"description": "LED bulb E27 9W", "quantity": "2000", "country_of_origin": "CN"},
        {"description": "Plush toy bear 30cm", "quantity": "500", "country_of_origin": "VN"},
    ]
    amounts = ("1000.00", "50.50", "9.50")
    return {
        CI: {
            "fields": {
                "invoice_number": "RE-26-3487",
                "seller_name": "Xiamen Tianyi Import & Export Co., Ltd.",
                "buyer_name": "Rogers-Hill",
                "incoterm": "FOB",
                "incoterm_place": "Ningbo",
                "total_amount": "1060.00",
            },
            "lines": [
                {**line, "amount": amount} for line, amount in zip(lines, amounts, strict=True)
            ],
        },
        PL: {
            "fields": {
                "invoice_number": "RE-26-3487",
                "seller_name": "XIAMEN TIANYI IMPORT & EXPORT CO LTD",
                "buyer_name": "Rogers-Hill",
                "incoterm": "FOB",
                "incoterm_place": "Ningbo",
                "total_packages": "345",
                "total_gross_weight_kg": "5137.03",
            },
            "lines": [dict(line) for line in lines],
        },
        BL: {
            "fields": {
                "seller_name": "Xiamen Tianyi Import & Export Co., Ltd.",
                "buyer_name": "Rogers-Hill",
                "total_packages": "345",
                "total_gross_weight_kg": "5137.0",  # within the weight tolerance
            },
        },
    }


def read(docs: dict[DocumentKind, Doc]) -> Reconciliation:
    readings = [
        DocumentReading(
            kind,
            {name: v(text) for name, text in doc["fields"].items()},
            [{name: v(text) for name, text in line.items()} for line in doc.get("lines", [])],
        )
        for kind, doc in docs.items()
    ]
    return reconcile(readings)


def found(result: Reconciliation) -> set[tuple[DiscrepancyKind, str, int | None]]:
    return {(f.kind, f.field_key, f.line_no) for f in result.findings}


def test_a_consistent_shipment_raises_nothing_and_corroborates() -> None:
    result = read(shipment())
    assert result.findings == []
    assert result.agreement[(CI, "invoice_number", None)] == "agree"
    assert result.agreement[(BL, "total_gross_weight_kg", None)] == "agree"
    assert result.agreement[(CI, "quantity", 2)] == "agree"
    assert result.agreement[(CI, "total_amount", None)] == "agree"  # equals the line sum
    assert result.support[(CI, "seller_name", None)] == [Decimal("0.9"), Decimal("0.9")]
    assert set(result.agreement.values()) == {"agree"}


Mutation = Callable[[dict[DocumentKind, Doc]], None]


def _set(kind: DocumentKind, name: str, text: str | None) -> Mutation:
    def mutate(docs: dict[DocumentKind, Doc]) -> None:
        docs[kind]["fields"][name] = text

    return mutate


def _set_line(kind: DocumentKind, index: int, name: str, text: str | None) -> Mutation:
    def mutate(docs: dict[DocumentKind, Doc]) -> None:
        docs[kind]["lines"][index][name] = text

    return mutate


def _drop_line(index: int) -> Mutation:
    def mutate(docs: dict[DocumentKind, Doc]) -> None:
        del docs[PL]["lines"][index]

    return mutate


# One per mutation kind in manifest_evals.synth.generate.MUTATIONS.
MUTATIONS: dict[str, tuple[Mutation, tuple[DiscrepancyKind, str, int | None]]] = {
    "gross_weight": (
        _set(BL, "total_gross_weight_kg", "5400.12"),
        (VALUE, "total_gross_weight_kg", None),
    ),
    "package_count": (_set(BL, "total_packages", "347"), (VALUE, "total_packages", None)),
    "line_quantity": (_set_line(PL, 1, "quantity", "1800"), (VALUE, "line_items.quantity", 2)),
    "missing_line": (_drop_line(1), (DiscrepancyKind.LINE_ITEMS_MISMATCH, "line_items", 2)),
    "invoice_total": (
        _set(CI, "total_amount", "1090.00"),
        (DiscrepancyKind.TOTALS_MISMATCH, "total_amount", None),
    ),
    "invoice_number": (_set(PL, "invoice_number", "RE-26-3478"), (VALUE, "invoice_number", None)),
    "consignee": (_set(BL, "buyer_name", "West, Cox and Parkinson"), (VALUE, "buyer_name", None)),
    "incoterm": (_set(PL, "incoterm", "CIF"), (VALUE, "incoterm", None)),
    "origin": (
        _set_line(PL, 2, "country_of_origin", "TH"),
        (VALUE, "line_items.country_of_origin", 3),
    ),
    "missing_origin": (
        _set_line(PL, 0, "country_of_origin", None),
        (DiscrepancyKind.MISSING_FIELD, "line_items.country_of_origin", 1),
    ),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_every_injected_mismatch_is_found_and_nothing_else(mutation: str) -> None:
    mutate, expected = MUTATIONS[mutation]
    docs = shipment()
    mutate(docs)
    result = read(docs)
    assert found(result) == {expected}
    [finding] = result.findings
    assert finding.message
    assert finding.observations
    missing = expected[0] is DiscrepancyKind.MISSING_FIELD
    expected_severity = DiscrepancySeverity.WARNING if missing else DiscrepancySeverity.BLOCKING
    assert finding.severity is expected_severity


def test_disagreement_is_recorded_but_agreeing_documents_still_corroborate() -> None:
    docs = shipment()
    _set(BL, "buyer_name", "Someone Else Ltd")(docs)
    result = read(docs)
    assert result.agreement[(BL, "buyer_name", None)] == "disagree"
    assert result.support[(BL, "buyer_name", None)] == []
    assert result.agreement[(CI, "buyer_name", None)] == "disagree"
    assert result.support[(CI, "buyer_name", None)] == [Decimal("0.9")]  # the packing list


def test_unread_values_are_not_compared() -> None:
    docs = shipment()
    _set(PL, "invoice_number", None)(docs)
    _set_line(PL, 1, "quantity", None)(docs)
    result = read(docs)
    assert result.findings == []
    assert result.agreement[(CI, "invoice_number", None)] == "single"


def test_counterparts_without_verified_evidence_dont_support() -> None:
    readings = [
        DocumentReading(CI, {"invoice_number": v("RE-26-3487")}),
        DocumentReading(PL, {"invoice_number": v("RE-26-3487", evidence="failed")}),
    ]
    result = reconcile(readings)
    assert result.agreement[(CI, "invoice_number", None)] == "agree"
    assert result.support[(CI, "invoice_number", None)] == []


def test_a_packing_list_line_missing_from_the_invoice() -> None:
    docs = shipment()
    extra = {"description": "Rubber flip-flops", "quantity": "60", "country_of_origin": "CN"}
    docs[PL]["lines"].append(extra)
    expected = (DiscrepancyKind.LINE_ITEMS_MISMATCH, "packing_list.line_items", 4)
    assert found(read(docs)) == {expected}


@pytest.mark.parametrize(
    ("a", "b", "comparison", "equal"),
    [
        ("Rogers-Hill", "ROGERS HILL", "name", True),
        ("Co., Ltd.", "co ltd", "name", True),
        ("40", "40.000", "number", True),
        ("40", "41", "number", False),
        ("100.0", "100.4", "weight", True),
        ("100.0", "100.6", "weight", False),
        ("CIF", "cif", "exact", False),
    ],
)
def test_comparisons(a: str, b: str, comparison: Any, equal: bool) -> None:
    assert same(a, b, comparison) is equal


def test_corroboration_sets_agreement_and_only_raises_confidence() -> None:
    docs = shipment()
    _set(BL, "buyer_name", "Someone Else Ltd")(docs)
    result = read(docs)
    invoice = DocumentReading(
        CI,
        {name: v(text) for name, text in docs[CI]["fields"].items()},
        [{name: v(text) for name, text in line.items()} for line in docs[CI]["lines"]],
    )
    updated = corroborated(invoice, result)
    number = updated.fields["invoice_number"]
    assert number.signals is not None
    assert number.signals.agreement == "agree"
    assert number.confidence > Decimal("0.98")  # the packing list agrees
    buyer = updated.fields["buyer_name"]
    assert buyer.signals is not None
    assert buyer.signals.agreement == "disagree"
    assert buyer.confidence > Decimal("0.9")  # the packing list still agrees; the B/L doesn't
    assert updated.lines[0]["quantity"].signals.agreement == "agree"  # type: ignore[union-attr]
    payment = corroborated(DocumentReading(CI, {"payment_terms": v("T/T")}), result)
    assert payment.fields["payment_terms"].signals.agreement == "single"  # type: ignore[union-attr]
    assert payment.fields["payment_terms"].confidence == Decimal("0.9")
