"""Shipment reconciliation and routing: idempotent, resolution-preserving, fail-closed."""

import threading
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import (
    Discrepancy,
    Document,
    Extraction,
    HsSuggestion,
    LineItem,
    ReviewTask,
    Shipment,
)
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.calibration import Calibrator, FitSummary
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, ShipmentStatus
from manifest_domain.review import DiscrepancyKind, ReviewReason
from manifest_domain.routing import decide
from manifest_domain.scoring import SCORE_VERSION
from manifest_worker import shipments

CI = DocumentKind.COMMERCIAL_INVOICE
PL = DocumentKind.PACKING_LIST
BL = DocumentKind.BILL_OF_LADING


def calibrator(provenance: str = "model", **changes: Any) -> Calibrator:
    fields: dict[str, Any] = {
        "name": "extraction",
        "version": 1,
        "provenance": provenance,
        "score_version": SCORE_VERSION,
        "schema_version": 3,
        "prompt_version": "test",
        "model": "anthropic/test",
        # A verified 0.97 reading calibrates to 0.985: above the threshold even uncorroborated.
        "knots": ((Decimal("0.5"), Decimal("0.5")), (Decimal("0.95"), Decimal("0.985"))),
        "threshold": Decimal("0.98"),
        "fitted_on": FitSummary(split="calibration", items=1, samples=1, positives=1),
    }
    return Calibrator.model_validate(fields | changes)


def read(value: str, raw: str = "0.97", evidence: str = "verified") -> dict[str, Any]:
    signals = {"self_report": raw, "evidence": evidence, "normaliser": "exact", "agreement": None}
    return {"page": 1, "evidence": value, "raw": raw, "signals": signals}


def extraction_for(
    document_id: UUID, fields: dict[str, str], lines: list[dict[str, str]] | None = None
) -> Extraction:
    lines = lines or []
    return Extraction(
        document_id=document_id,
        schema_version=3,
        payload={"fields": fields, "line_items": lines},
        field_confidence={
            "fields": dict.fromkeys(fields, "0.97"),
            "line_items": [dict.fromkeys(line, "0.97") for line in lines],
        },
        provenance={
            "fields": {k: read(v) for k, v in fields.items()},
            "line_items": [{k: read(v) for k, v in line.items()} for line in lines],
        },
        model="test",
        tokens_in=1,
        tokens_out=1,
    )


INVOICE = {
    "invoice_number": "INV-1",
    "invoice_date": "2026-01-05",
    "seller_name": "Acme Ltd",
    "buyer_name": "Rogers-Hill",
    "currency": "USD",
    "incoterm": "FOB",
    "total_amount": "100.00",
}
PACKING = {
    "invoice_number": "INV-1",
    "seller_name": "Acme Ltd",
    "buyer_name": "Rogers-Hill",
    "total_packages": "3",
    "total_gross_weight_kg": "20.00",
}
LADING = {
    "bl_number": "BL-1",
    "seller_name": "Acme Ltd",
    "buyer_name": "Rogers-Hill",
    "port_of_loading": "Ningbo",
    "port_of_discharge": "Hamburg",
    "total_packages": "3",
    "total_gross_weight_kg": "20.00",
}
INVOICE_LINES = [
    {"description": "Laptop", "quantity": "2", "amount": "100.00", "country_of_origin": "CN"}
]
PACKING_LINES = [{"description": "Laptop", "quantity": "2", "country_of_origin": "CN"}]


# ---------------------------------------------------------------- without a database


def current(document_id: UUID, status: DocumentStatus, fields: dict[str, str]) -> shipments.Current:
    document = Document(id=document_id, kind=CI, status=status)
    extraction = extraction_for(document_id, fields)
    extraction.id = uuid4()
    return shipments.Current(document, extraction)


def test_the_fingerprint_follows_inputs_not_what_reconciliation_writes() -> None:
    a, b = uuid4(), uuid4()
    first = [
        current(a, DocumentStatus.EXTRACTED, INVOICE),
        current(b, DocumentStatus.EXTRACTED, PACKING),
    ]
    stamp = shipments.fingerprint(first)
    assert shipments.fingerprint(list(reversed(first))) == stamp  # order doesn't matter
    rescored = first[0].extraction
    assert rescored is not None
    rescored.field_confidence = {"fields": {"invoice_number": "0.42"}}
    rescored.provenance = {**(rescored.provenance or {}), "shipment": {"fields": {}}}
    assert shipments.fingerprint(first) == stamp  # reconciliation's own writes
    changed = {**INVOICE, "total_amount": "101.00"}
    assert shipments.fingerprint([current(a, DocumentStatus.EXTRACTED, changed), first[1]]) != stamp


def test_stored_extractions_read_back_with_raw_scores_and_signals() -> None:
    extraction = extraction_for(uuid4(), INVOICE, INVOICE_LINES)
    reading = shipments.reading(extraction, CI)
    number = reading.fields["invoice_number"]
    assert (number.value, number.confidence, number.page) == ("INV-1", Decimal("0.97"), 1)
    assert number.signals is not None
    assert number.signals.evidence == "verified"
    assert reading.lines[0]["country_of_origin"].value == "CN"
    older = Extraction(
        payload={"fields": {"invoice_number": "INV-1"}, "line_items": []},
        field_confidence={"fields": {"invoice_number": "0.8"}, "line_items": []},
    )
    legacy = shipments.reading(older, CI).fields["invoice_number"]
    assert (legacy.confidence, legacy.signals) == (Decimal("0.8"), None)


@pytest.mark.parametrize(
    ("fitted", "threshold"),
    [
        (calibrator(), Decimal("0.98")),
        (calibrator("oracle"), None),
        (calibrator(score_version=SCORE_VERSION + 1), None),
        (calibrator(schema_version=2), None),
        (None, None),
    ],
)
def test_only_a_model_fitted_current_calibrator_sets_a_threshold(
    fitted: Calibrator | None, threshold: Decimal | None
) -> None:
    assert shipments.trusted_threshold(fitted) == threshold


# ---------------------------------------------------------------- against Postgres


@pytest.fixture
def sessions(postgres_url: str) -> Iterator[sessionmaker[Session]]:
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        yield sync_session_factory(database)


Documents = dict[DocumentKind, tuple[dict[str, str], list[dict[str, str]]]]


def add_shipment(
    sessions: sessionmaker[Session],
    documents: Documents,
    status: DocumentStatus = DocumentStatus.EXTRACTED,
) -> tuple[UUID, dict[DocumentKind, UUID]]:
    with sessions.begin() as session:
        shipment = Shipment(reference="test", status=ShipmentStatus.PROCESSING)
        session.add(shipment)
        session.flush()
        ids = {}
        for kind, (fields, lines) in documents.items():
            document = Document(
                shipment_id=shipment.id,
                kind=kind,
                status=status,
                filename=f"{kind}.pdf",
                content_type="application/pdf",
                size_bytes=1,
                storage_key=f"test/{uuid4()}",
                page_count=1,
            )
            session.add(document)
            session.flush()
            extraction = extraction_for(document.id, fields, lines)
            session.add(extraction)
            session.flush()
            for number, line in enumerate(lines, start=1):
                session.add(
                    LineItem(
                        extraction_id=extraction.id,
                        line_no=number,
                        description=line.get("description"),
                        country_of_origin=line.get("country_of_origin"),
                    )
                )
            ids[kind] = document.id
        return shipment.id, ids


CONSISTENT: Documents = {
    CI: (INVOICE, INVOICE_LINES),
    PL: (PACKING, PACKING_LINES),
    BL: (LADING, []),
}


def reconcile(sessions: sessionmaker[Session], shipment_id: UUID) -> shipments.Reconciled:
    with sessions.begin() as session:
        return shipments.reconcile_shipment(session, shipment_id, calibrator())


def discrepancies(sessions: sessionmaker[Session], shipment_id: UUID) -> list[Discrepancy]:
    with sessions() as session:
        query = select(Discrepancy).where(Discrepancy.shipment_id == shipment_id)
        return list(session.scalars(query.order_by(Discrepancy.field_key)))


@pytest.mark.integration
def test_waits_until_every_document_is_done(sessions: sessionmaker[Session]) -> None:
    shipment_id, _ = add_shipment(sessions, CONSISTENT, DocumentStatus.PROCESSING)
    assert reconcile(sessions, shipment_id).skipped == "documents still in progress"


@pytest.mark.integration
def test_reconciles_once_and_rescores_with_agreement(sessions: sessionmaker[Session]) -> None:
    shipment_id, ids = add_shipment(sessions, CONSISTENT)
    first = reconcile(sessions, shipment_id)
    assert (first.skipped, first.findings) == (None, [])
    assert reconcile(sessions, shipment_id).skipped == "already reconciled"
    with sessions() as session:
        query = select(Extraction).where(Extraction.document_id == ids[CI])
        extraction = session.scalars(query).one()
        assert extraction.calibrator == "extraction-v1"
        assert extraction.provenance is not None
        agreement = extraction.provenance["shipment"]["fields"]["invoice_number"]["agreement"]
        assert agreement == "agree"
        # 0.97 corroborated by the packing list's 0.97, then calibrated.
        assert Decimal(extraction.field_confidence["fields"]["invoice_number"]) == Decimal("0.985")
        raw = extraction.provenance["shipment"]["fields"]["invoice_number"]["raw"]
        assert Decimal(raw) > Decimal("0.99")
        shipment = session.get(Shipment, shipment_id)
        assert shipment is not None
        assert shipment.reconciled_fingerprint == first.fingerprint


@pytest.mark.integration
def test_discrepancies_upsert_and_keep_resolutions(sessions: sessionmaker[Session]) -> None:
    mismatched = dict(CONSISTENT)
    mismatched[BL] = ({**LADING, "total_packages": "4", "buyer_name": "Someone Else"}, [])
    shipment_id, ids = add_shipment(sessions, mismatched)
    reconcile(sessions, shipment_id)
    found = discrepancies(sessions, shipment_id)
    assert [(d.kind, d.field_key) for d in found] == [
        (DiscrepancyKind.VALUE_MISMATCH, "buyer_name"),
        (DiscrepancyKind.VALUE_MISMATCH, "total_packages"),
    ]
    assert {o["document"] for o in found[1].observations} == {"packing_list", "bill_of_lading"}
    with sessions.begin() as session:  # a reviewer resolves the package count
        resolved = session.get(Discrepancy, found[1].id)
        assert resolved is not None
        resolved.resolved_at = func.now()
    with sessions.begin() as session:  # the B/L is re-extracted: its buyer now agrees
        query = select(Extraction).where(Extraction.document_id == ids[BL])
        extraction = session.scalars(query).one()
        extraction.payload = {"fields": {**LADING, "total_packages": "4"}, "line_items": []}
    reconcile(sessions, shipment_id)
    after = discrepancies(sessions, shipment_id)
    assert [(d.field_key, d.resolved_at is not None) for d in after] == [("total_packages", True)]


@pytest.mark.integration
def test_concurrent_runs_neither_deadlock_nor_duplicate(sessions: sessionmaker[Session]) -> None:
    mismatched = dict(CONSISTENT)
    mismatched[PL] = ({**PACKING, "invoice_number": "INV-2"}, PACKING_LINES)
    shipment_id, _ = add_shipment(sessions, mismatched)
    outcomes: list[shipments.Reconciled] = []

    def run() -> None:
        outcomes.append(reconcile(sessions, shipment_id))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(o.skipped or "ran" for o in outcomes) == ["already reconciled"] * 3 + ["ran"]
    assert len(discrepancies(sessions, shipment_id)) == 1


def route(sessions: sessionmaker[Session], shipment_id: UUID, hs: Calibrator | None) -> Any:
    with sessions.begin() as session:
        found = shipments.results(session, shipment_id, (calibrator(), hs), classified=True)
        decision = decide(found)
        return shipments.route(session, shipment_id, decision), decision


def classify_all(sessions: sessionmaker[Session], shipment_id: UUID, confidence: str) -> None:
    with sessions.begin() as session:
        for line in shipments.lines_to_classify(session, shipment_id):
            session.add(
                HsSuggestion(
                    line_item_id=line.line_item_id,
                    code="847130",
                    confidence=Decimal(confidence),
                    rationale="test",
                    model="test",
                )
            )


@pytest.mark.integration
def test_routing_auto_approves_only_when_everything_clears(
    sessions: sessionmaker[Session],
) -> None:
    shipment_id, _ = add_shipment(sessions, CONSISTENT)
    reconcile(sessions, shipment_id)
    classify_all(sessions, shipment_id, "0.99")
    hs = calibrator(name="hs", threshold=Decimal("0.95"))
    status, decision = route(sessions, shipment_id, hs)
    assert (status, decision.reasons) == (ShipmentStatus.AUTO_APPROVED, frozenset())


@pytest.mark.integration
def test_routing_fails_closed_and_respects_reviewers(sessions: sessionmaker[Session]) -> None:
    shipment_id, _ = add_shipment(sessions, CONSISTENT)
    reconcile(sessions, shipment_id)
    classify_all(sessions, shipment_id, "0.99")
    status, decision = route(sessions, shipment_id, calibrator("oracle", name="hs"))
    assert status is ShipmentStatus.NEEDS_REVIEW
    assert decision.reasons == {ReviewReason.HS_UNCERTAIN}  # oracle-fitted: never trusted
    hs = calibrator(name="hs", threshold=Decimal("0.95"))
    status, _ = route(sessions, shipment_id, hs)
    assert status is ShipmentStatus.NEEDS_REVIEW  # clear now, but a review task is open
    with sessions.begin() as session:
        shipment = session.get(Shipment, shipment_id)
        assert shipment is not None
        shipment.status = ShipmentStatus.APPROVED
        task = session.scalars(select(ReviewTask)).one()
        assert task.reasons == [ReviewReason.HS_UNCERTAIN]
    status, _ = route(sessions, shipment_id, calibrator("oracle", name="hs"))
    assert status is ShipmentStatus.APPROVED  # a reviewer's approval stands
