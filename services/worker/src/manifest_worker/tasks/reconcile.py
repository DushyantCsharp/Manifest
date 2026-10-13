"""Queue ``reconcile``: merge a document's pages, then reconcile the shipment's documents.

``reconcile`` is the chord callback: it runs once every page task has returned, whatever
happened to them, and works with the pages that succeeded. A document with failed pages is
``partially_extracted`` and goes to review; with no successful pages it is ``failed``. Keyed by
(document, schema version): a rerun replaces that version's extraction instead of adding
another. It always hands over to ``reconcile_shipment``.

``reconcile_shipment`` waits until every document of the shipment is done, then compares them
(``manifest_worker.shipments``): discrepancies, agreement, calibrated confidences. It hands
over to ``hs_classify``, which classifies the goods lines and routes the shipment.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from manifest_db.models import Document, Extraction, LineItem, Page, PageExtraction
from manifest_domain.calibration import Calibrator
from manifest_domain.extraction import LINE_FIELDS, PROVENANCE_SINCE, ExtractedValue, PageFragment
from manifest_domain.lifecycle import DocumentStatus, PageStatus
from manifest_domain.merge import Merged, merge
from manifest_domain.review import ReviewReason
from manifest_domain.scoring import final
from manifest_worker import shipments
from manifest_worker.app import celery_app
from manifest_worker.pipeline import PipelineTask
from manifest_worker.records import open_review
from manifest_worker.runtime import runtime
from manifest_worker.tasks.hs_classify import hs_classify

MONEY = ("unit_price", "amount")


def _line_columns(line: dict[str, ExtractedValue], schema_version: int) -> dict[str, Any]:
    kinds = LINE_FIELDS[schema_version]
    columns: dict[str, Any] = {}
    for name, extracted in line.items():
        value = extracted.value
        columns[name] = Decimal(value) if value is not None and kinds[name] == "decimal" else value
    return columns


def _provenance(value: ExtractedValue) -> dict[str, Any]:
    return {
        "page": value.page,
        "evidence": value.evidence,
        "raw": str(value.confidence),
        "signals": value.signals.model_dump(mode="json") if value.signals else None,
    }


def _scored(
    merged: Merged, schema_version: int, calibrator: Calibrator | None
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """(field_confidence, provenance, calibrator label). v3 confidences are calibrated when a
    calibrator is pinned and zeroed without verified evidence; agreement across documents is
    added by shipment reconciliation. Older versions keep their raw confidences."""
    if schema_version < PROVENANCE_SINCE:
        raw = {
            "fields": {name: str(value.confidence) for name, value in merged.fields.items()},
            "line_items": [
                {k: str(v.confidence) for k, v in line.items()} for line in merged.lines
            ],
        }
        return raw, None, None
    apply = calibrator.apply if calibrator is not None else None

    def scored(value: ExtractedValue) -> str:
        return str(final(value, apply))

    confidence = {
        "fields": {name: scored(value) for name, value in merged.fields.items()},
        "line_items": [{k: scored(v) for k, v in line.items()} for line in merged.lines],
    }
    provenance = {
        "fields": {name: _provenance(value) for name, value in merged.fields.items()},
        "line_items": [{k: _provenance(v) for k, v in line.items()} for line in merged.lines],
    }
    return confidence, provenance, calibrator.label if calibrator is not None else None


def _store(
    session: Session,
    document: Document,
    schema_version: int,
    merged: Merged,
    pages: list[PageExtraction],
    calibrator: Calibrator | None,
) -> tuple[int, bool]:
    """Write this version's extraction and line items. Returns (lines, money lacked currency)."""
    current = session.scalar(
        select(Extraction)
        .where(Extraction.document_id == document.id, Extraction.superseded_at.is_(None))
        .with_for_update()
    )
    if current is not None and current.schema_version == schema_version:
        extraction = current  # same version: overwrite in place
        session.execute(delete(LineItem).where(LineItem.extraction_id == current.id))
    else:
        if current is not None:
            current.superseded_at = func.now()
            session.flush()  # only one current extraction per document
        extraction = Extraction(document_id=document.id, schema_version=schema_version)
        session.add(extraction)
    extraction.payload = {
        "fields": {name: value.value for name, value in merged.fields.items()},
        "line_items": [{k: v.value for k, v in line.items()} for line in merged.lines],
    }
    confidence, provenance, label = _scored(merged, schema_version, calibrator)
    extraction.field_confidence = confidence
    extraction.provenance = provenance
    extraction.calibrator = label
    extraction.model = ",".join(sorted({page.model or "unknown" for page in pages}))
    extraction.tokens_in = sum(page.tokens_in or 0 for page in pages)
    extraction.tokens_out = sum(page.tokens_out or 0 for page in pages)
    session.flush()

    header_currency = merged.fields.get("currency", ExtractedValue(confidence=Decimal(0))).value
    money_without_currency = False
    for line_no, line in enumerate(merged.lines, start=1):
        columns = _line_columns(line, schema_version)
        columns["currency"] = columns.get("currency") or header_currency
        if columns["currency"] is None and any(columns.get(name) is not None for name in MONEY):
            # Money is an amount plus a currency: without one, keep it in the payload only.
            money_without_currency = True
            columns.update(dict.fromkeys(MONEY))
        session.add(LineItem(extraction_id=extraction.id, line_no=line_no, **columns))
    return len(merged.lines), money_without_currency


@celery_app.task(bind=True, base=PipelineTask, name="manifest.reconcile", stage="reconcile")
def reconcile(
    self: PipelineTask, page_results: list[Any] | None, document_id: str, schema_version: int
) -> dict[str, Any]:
    rt = runtime()
    doc_id = UUID(document_id)
    with rt.sessions.begin() as session:
        document = session.get(Document, doc_id, with_for_update=True)
        if document is None:
            return {"document_id": document_id, "skipped": "unknown document"}
        page_nos = sorted(session.scalars(select(Page.page_no).where(Page.document_id == doc_id)))
        results = {
            row.page_no: row
            for row in session.scalars(
                select(PageExtraction).where(
                    PageExtraction.document_id == doc_id,
                    PageExtraction.schema_version == schema_version,
                )
            )
        }
        extracted = [
            results[n]
            for n in page_nos
            if n in results and results[n].status is PageStatus.EXTRACTED
        ]
        reasons: set[ReviewReason] = set()
        if len(extracted) < len(page_nos):
            reasons.add(ReviewReason.PARTIAL_EXTRACTION)

        lines = 0
        if extracted:
            fragments = [PageFragment.model_validate(row.fragment) for row in extracted]
            calibrator = rt.calibrators.extraction
            lines, lacks_currency = _store(
                session, document, schema_version, merge(fragments), extracted, calibrator
            )
            if lacks_currency:
                reasons.add(ReviewReason.MISSING_FIELD)
            partial = ReviewReason.PARTIAL_EXTRACTION in reasons
            document.status = (
                DocumentStatus.PARTIALLY_EXTRACTED if partial else DocumentStatus.EXTRACTED
            )
        else:
            document.status = DocumentStatus.FAILED
        if reasons:
            open_review(session, document.shipment_id, reasons)
        status, shipment_id = document.status, document.shipment_id

    # Always, once committed: the shipment decides whether all its documents are done.
    reconcile_shipment.delay(str(shipment_id))
    return {
        "document_id": document_id,
        "status": str(status),
        "pages_extracted": len(extracted),
        "pages": len(page_nos),
        "lines": lines,
    }


@celery_app.task(
    bind=True, base=PipelineTask, name="manifest.reconcile_shipment", stage="reconcile"
)
def reconcile_shipment(self: PipelineTask, shipment_id: str) -> dict[str, Any]:
    rt = runtime()
    with rt.sessions.begin() as session:
        result = shipments.reconcile_shipment(session, UUID(shipment_id), rt.calibrators.extraction)
    if result.skipped is not None:
        return {"shipment_id": shipment_id, "skipped": result.skipped}
    hs_classify.delay(shipment_id, result.fingerprint)
    return {
        "shipment_id": shipment_id,
        "fingerprint": result.fingerprint,
        "discrepancies": len(result.findings),
    }
