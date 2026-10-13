"""Shipment-level work: cross-document reconciliation, calibrated confidences, routing.

Once every document of a shipment is done, ``reconcile_shipment`` compares them
(``manifest_domain.reconciliation``), records the discrepancies, and rescores every value with
the agreement of the other documents and the pinned calibrator. ``hs_classify`` then
classifies the goods lines and ``route`` decides: review, or auto-approve.

Idempotent by construction: a shipment is reconciled against a *fingerprint* of its current
extractions, so a repeated delivery finds nothing new to do and a run that a newer extraction
overtook stops. Discrepancies are upserted on their natural key (shipment, kind, field, line),
so a reviewer's resolution survives a re-run and only discrepancies no longer found (and not
yet resolved) are deleted.

Locks are taken in the order the per-document stages use (documents, by id, then their
extractions, then review task and shipment), so concurrent stages wait instead of deadlocking.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from manifest_db.models import (
    Discrepancy,
    Document,
    Extraction,
    HsSuggestion,
    LineItem,
    ReviewTask,
    Shipment,
)
from manifest_domain.calibration import Calibrator
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION, ExtractedValue, Signals
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, ShipmentStatus
from manifest_domain.reconciliation import DocumentReading, Finding, corroborated, reconcile
from manifest_domain.review import ReviewTaskStatus
from manifest_domain.routing import Decision, ShipmentResults, missing_required
from manifest_domain.scoring import SCORE_VERSION, final
from manifest_worker.ports import LineToClassify
from manifest_worker.records import open_review

TERMINAL = frozenset(
    {DocumentStatus.EXTRACTED, DocumentStatus.PARTIALLY_EXTRACTED, DocumentStatus.FAILED}
)
PARTIAL = frozenset({DocumentStatus.PARTIALLY_EXTRACTED, DocumentStatus.FAILED})
# Goods lines are classified on the invoice; on the packing list if there is no invoice.
HS_SOURCES = (DocumentKind.COMMERCIAL_INVOICE, DocumentKind.PACKING_LIST)
Apply = Callable[[Decimal], Decimal] | None


@dataclass(frozen=True)
class Current:
    """A document and its current extraction, read under lock."""

    document: Document
    extraction: Extraction | None


def lock(session: Session, shipment_id: UUID) -> tuple[Shipment | None, list[Current]]:
    """The shipment's documents and current extractions, locked in pipeline order."""
    documents = session.scalars(
        select(Document)
        .where(Document.shipment_id == shipment_id)
        .order_by(Document.id)
        .with_for_update()
    ).all()
    ids = [document.id for document in documents]
    extractions = {
        extraction.document_id: extraction
        for extraction in session.scalars(
            select(Extraction)
            .where(Extraction.document_id.in_(ids), Extraction.superseded_at.is_(None))
            .order_by(Extraction.document_id)
            .with_for_update()
        )
    }
    shipment = session.get(Shipment, shipment_id, with_for_update=True)
    return shipment, [Current(d, extractions.get(d.id)) for d in documents]


def fingerprint(current: Sequence[Current]) -> str:
    """What reconciliation is computed from: each document's state and current extraction
    (its values and page-level provenance, not what reconciliation itself writes)."""
    parts = []
    for item in sorted(current, key=lambda c: str(c.document.id)):
        state: dict[str, Any] = {"document": str(item.document.id), "status": item.document.status}
        if item.extraction is not None:
            provenance = item.extraction.provenance or {}
            state |= {
                "extraction": str(item.extraction.id),
                "schema_version": item.extraction.schema_version,
                "payload": item.extraction.payload,
                "read": {key: provenance.get(key) for key in ("fields", "line_items")},
            }
        parts.append(state)
    encoded = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _value(text: str | None, read: dict[str, Any] | None, stored: str | None) -> ExtractedValue:
    """A value as reconcile stored it: raw score and signals from provenance (schema v3), else
    the stored confidence."""
    if not read:
        return ExtractedValue(value=text, confidence=Decimal(stored or 0))
    signals = Signals.model_validate(read["signals"]) if read.get("signals") else None
    return ExtractedValue(
        value=text,
        confidence=Decimal(read["raw"]),
        page=read.get("page"),
        evidence=read.get("evidence"),
        signals=signals,
    )


def reading(extraction: Extraction, kind: DocumentKind) -> DocumentReading:
    """A stored extraction as reconciliation reads it."""
    payload, provenance = extraction.payload, extraction.provenance or {}
    stored = extraction.field_confidence or {}
    read_fields, stored_fields = provenance.get("fields", {}), stored.get("fields", {})
    fields = {
        name: _value(text, read_fields.get(name), stored_fields.get(name))
        for name, text in payload.get("fields", {}).items()
    }
    read_lines, stored_lines = provenance.get("line_items", []), stored.get("line_items", [])
    lines = []
    for index, line in enumerate(payload.get("line_items", [])):
        read = read_lines[index] if index < len(read_lines) else {}
        kept = stored_lines[index] if index < len(stored_lines) else {}
        lines.append({k: _value(text, read.get(k), kept.get(k)) for k, text in line.items()})
    return DocumentReading(kind, fields, lines)


def trusted_threshold(calibrator: Calibrator | None) -> Decimal | None:
    """The calibrator's auto-approve threshold, if routing may rely on it: fitted on a real
    model's answers, to this scoring recipe (and, for extraction, schema). Otherwise None,
    and nothing auto-approves."""
    if calibrator is None or not calibrator.trusted or calibrator.score_version != SCORE_VERSION:
        return None
    if calibrator.name == "extraction" and calibrator.schema_version != CURRENT_SCHEMA_VERSION:
        return None
    return calibrator.threshold


def _observations(finding: Finding, ids: dict[DocumentKind, UUID]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": str(ids[o.document]) if o.document in ids else None,
            "document": o.document.value,
            "value": o.value,
            "page": o.page,
            "line_no": o.line_no,
        }
        for o in finding.observations
    ]


def record_findings(
    session: Session,
    shipment_id: UUID,
    findings: Sequence[Finding],
    ids: dict[DocumentKind, UUID],
) -> None:
    """Upsert this reconciliation's discrepancies; drop unresolved ones no longer found."""
    found = set()
    for finding in findings:
        key = {
            "shipment_id": shipment_id,
            "kind": finding.kind,
            "field_key": finding.field_key,
            "line_no": finding.line_no,
        }
        values = {
            "severity": finding.severity,
            "message": finding.message,
            "observations": _observations(finding, ids),
        }
        statement = insert(Discrepancy).values(**key, **values)
        session.execute(statement.on_conflict_do_update(index_elements=list(key), set_=values))
        found.add((finding.kind, finding.field_key, finding.line_no))
    unresolved = select(Discrepancy).where(
        Discrepancy.shipment_id == shipment_id, Discrepancy.resolved_at.is_(None)
    )
    for discrepancy in session.scalars(unresolved):
        if (discrepancy.kind, discrepancy.field_key, discrepancy.line_no) not in found:
            session.delete(discrepancy)


def _rescored(document: DocumentReading, apply: Apply) -> tuple[dict[str, Any], dict[str, Any]]:
    """(field_confidence, provenance["shipment"]) of a corroborated document."""

    def agreement(value: ExtractedValue) -> dict[str, Any]:
        signals = value.signals
        return {"agreement": signals.agreement if signals else None, "raw": str(value.confidence)}

    lines = document.lines
    confidence = {
        "fields": {name: str(final(v, apply)) for name, v in document.fields.items()},
        "line_items": [{k: str(final(v, apply)) for k, v in line.items()} for line in lines],
    }
    shipment = {
        "fields": {name: agreement(v) for name, v in document.fields.items()},
        "line_items": [{k: agreement(v) for k, v in line.items()} for line in lines],
    }
    return confidence, shipment


@dataclass(frozen=True)
class Reconciled:
    fingerprint: str
    findings: list[Finding]
    skipped: str | None = None


def reconcile_shipment(
    session: Session, shipment_id: UUID, calibrator: Calibrator | None
) -> Reconciled:
    """Reconcile the shipment's documents and rescore their values (within ``session``)."""
    shipment, current = lock(session, shipment_id)
    if shipment is None:
        return Reconciled("", [], "unknown shipment")
    if not current or any(c.document.status not in TERMINAL for c in current):
        return Reconciled("", [], "documents still in progress")
    stamp = fingerprint(current)
    if shipment.reconciled_fingerprint == stamp:
        return Reconciled(stamp, [], "already reconciled")
    readings: dict[UUID, DocumentReading] = {}
    ids: dict[DocumentKind, UUID] = {}
    for item in current:
        if item.extraction is not None and item.document.kind is not None:
            readings[item.document.id] = reading(item.extraction, item.document.kind)
            ids.setdefault(item.document.kind, item.document.id)
    result = reconcile(list(readings.values()))
    record_findings(session, shipment_id, result.findings, ids)
    apply = calibrator.apply if calibrator is not None else None
    for item in current:
        if item.extraction is None or item.document.id not in readings:
            continue
        confidence, agreement = _rescored(corroborated(readings[item.document.id], result), apply)
        item.extraction.field_confidence = confidence
        if item.extraction.provenance is not None:  # schema v3: record the agreement too
            item.extraction.provenance = {**item.extraction.provenance, "shipment": agreement}
            item.extraction.calibrator = calibrator.label if calibrator is not None else None
    shipment.reconciled_fingerprint = stamp
    return Reconciled(stamp, list(result.findings))


def hs_source(session: Session, shipment_id: UUID) -> DocumentKind | None:
    """Which document's lines are classified: the invoice's, else the packing list's."""
    kinds = set(session.scalars(select(Document.kind).where(Document.shipment_id == shipment_id)))
    return next((kind for kind in HS_SOURCES if kind in kinds), None)


def _goods(session: Session, shipment_id: UUID) -> Any:
    """Current goods lines of the HS source document, with their suggestion (if any)."""
    return (
        select(LineItem, HsSuggestion.confidence)
        .join(Extraction, Extraction.id == LineItem.extraction_id)
        .join(Document, Document.id == Extraction.document_id)
        .outerjoin(HsSuggestion, HsSuggestion.line_item_id == LineItem.id)
        .where(
            Document.shipment_id == shipment_id,
            Document.kind == hs_source(session, shipment_id),
            Extraction.superseded_at.is_(None),
        )
        .order_by(Document.id, LineItem.line_no)
    )


def lines_to_classify(session: Session, shipment_id: UUID) -> list[LineToClassify]:
    """Goods lines without an HS suggestion yet (a re-run after a deferral skips the rest)."""
    return [
        LineToClassify(line.id, line.description, line.country_of_origin)
        for line, confidence in session.execute(_goods(session, shipment_id))
        if confidence is None
    ]


def results(
    session: Session,
    shipment_id: UUID,
    calibrators: tuple[Calibrator | None, Calibrator | None],
    *,
    classified: bool,
) -> ShipmentResults:
    """What routing decides on, from what the stages stored."""
    extraction_calibrator, hs_calibrator = calibrators
    documents = session.scalars(select(Document).where(Document.shipment_id == shipment_id)).all()
    confidences: list[Decimal] = []
    missing = False
    all_v3 = True
    for document in documents:
        extraction = session.scalar(
            select(Extraction).where(
                Extraction.document_id == document.id, Extraction.superseded_at.is_(None)
            )
        )
        if extraction is None or document.kind is None:
            continue
        all_v3 &= extraction.provenance is not None
        stored = extraction.field_confidence or {}
        confidences += [Decimal(c) for c in stored.get("fields", {}).values()]
        confidences += [Decimal(c) for line in stored.get("line_items", []) for c in line.values()]
        fields = reading(extraction, document.kind).fields
        missing |= bool(missing_required(document.kind, fields))
    unresolved = session.scalars(
        select(Discrepancy.kind).where(
            Discrepancy.shipment_id == shipment_id, Discrepancy.resolved_at.is_(None)
        )
    ).all()
    hs = None
    if classified:
        rows = session.execute(_goods(session, shipment_id)).all()
        hs = [confidence if confidence is not None else Decimal(0) for _, confidence in rows]
    return ShipmentResults(
        partial=any(document.status in PARTIAL for document in documents),
        discrepancies=list(unresolved),
        missing_required=missing,
        confidences=confidences,
        hs_confidences=hs,
        extraction_threshold=trusted_threshold(extraction_calibrator) if all_v3 else None,
        hs_threshold=trusted_threshold(hs_calibrator),
    )


def route(session: Session, shipment_id: UUID, decision: Decision) -> ShipmentStatus | None:
    """Apply a routing decision. Never overrides a reviewer's approval, and never
    auto-approves while a review task is open (lock order: review task, then shipment)."""
    current = session.get(Shipment, shipment_id)
    if current is None or current.status is ShipmentStatus.APPROVED:
        return None if current is None else current.status
    if decision.reasons:
        open_review(session, shipment_id, decision.reasons, decision.priority)
        return ShipmentStatus.NEEDS_REVIEW
    task = session.scalar(
        select(ReviewTask)
        .where(ReviewTask.shipment_id == shipment_id)
        .where(ReviewTask.status != ReviewTaskStatus.APPROVED)
        .with_for_update()
    )
    shipment = session.get(Shipment, shipment_id, with_for_update=True, populate_existing=True)
    if shipment is None:
        return None
    if task is None:
        shipment.status = ShipmentStatus.AUTO_APPROVED
    else:
        shipment.status = ShipmentStatus.NEEDS_REVIEW
    return shipment.status
