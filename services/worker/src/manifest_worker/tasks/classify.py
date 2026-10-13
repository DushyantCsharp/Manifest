"""Queue ``classify``: split a document into pages, classify it, fan out one task per page.

The fan-out is a chord: ``page_extract`` for every page, then ``reconcile`` once all of them
have returned (page tasks always return, so reconcile runs even when pages fail).
"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from celery import chord
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from manifest_db.models import Document, Extraction, Page, Shipment
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION
from manifest_domain.lifecycle import DocumentStatus, PageStatus, ShipmentStatus
from manifest_domain.review import ReviewReason
from manifest_worker.app import celery_app
from manifest_worker.errors import ContentError, NotConfiguredError
from manifest_worker.pipeline import PipelineTask
from manifest_worker.records import open_review
from manifest_worker.runtime import Runtime, runtime
from manifest_worker.storage import page_key
from manifest_worker.tasks.page_extract import page_extract
from manifest_worker.tasks.reconcile import reconcile
from manifest_worker.text import classify_kind, native_text, split_pages


def launch_extraction(document_id: UUID, page_nos: Iterable[int], schema_version: int) -> None:
    header = [page_extract.s(str(document_id), n, schema_version) for n in page_nos]
    chord(header)(reconcile.s(str(document_id), schema_version))


def _first_page_text(rt: Runtime, page_pdf: bytes) -> str:
    text = native_text(page_pdf)
    if len(text.strip()) >= rt.ocr_min_chars:
        return text
    try:
        return rt.ocr(page_pdf)
    except NotConfiguredError:
        return text


@celery_app.task(bind=True, base=PipelineTask, name="manifest.classify", stage="classify")
def classify(
    self: PipelineTask, document_id: str, schema_version: int = CURRENT_SCHEMA_VERSION
) -> dict[str, Any]:
    rt = runtime()
    doc_id = UUID(document_id)
    with rt.sessions.begin() as session:
        document = session.get(Document, doc_id)
        if document is None:
            return {"document_id": document_id, "skipped": "unknown document"}
        storage_key, shipment_id = document.storage_key, document.shipment_id
        document.status = DocumentStatus.PROCESSING
        shipment = session.get(Shipment, shipment_id)
        if shipment is not None and shipment.status is ShipmentStatus.OPEN:
            shipment.status = ShipmentStatus.PROCESSING

    try:
        pages = split_pages(rt.storage.get(storage_key))
    except ContentError as exc:  # not a readable PDF: nothing to retry, a person must look
        with rt.sessions.begin() as session:
            failed = session.get(Document, doc_id)
            if failed is not None:
                failed.status = DocumentStatus.FAILED
            open_review(session, shipment_id, {ReviewReason.PARTIAL_EXTRACTION})
        return {"document_id": document_id, "status": "failed", "error": str(exc)}

    kind = classify_kind(_first_page_text(rt, pages[0]))
    for page_no, data in enumerate(pages, start=1):
        rt.storage.put(page_key(doc_id, page_no), data, "application/pdf")

    with rt.sessions.begin() as session:
        document = session.get(Document, doc_id)
        if document is not None:
            document.kind = document.kind or kind
            document.page_count = len(pages)
        rows = [{"document_id": doc_id, "page_no": n} for n in range(1, len(pages) + 1)]
        statement = insert(Page).values(rows)
        reset = {"status": PageStatus.PENDING, "attempts": 0, "error": None, "text": None}
        session.execute(
            statement.on_conflict_do_update(index_elements=["document_id", "page_no"], set_=reset)
        )

    launch_extraction(doc_id, range(1, len(pages) + 1), schema_version)
    return {"document_id": document_id, "kind": str(kind), "pages": len(pages)}


@celery_app.task(bind=True, base=PipelineTask, name="manifest.reextract_document", stage="classify")
def reextract_document(self: PipelineTask, document_id: str, schema_version: int) -> dict[str, Any]:
    """Re-extract a document's existing pages into ``schema_version`` (backfills).

    Idempotent: a no-op once the document's current extraction is at that version or later.
    """
    rt = runtime()
    doc_id = UUID(document_id)
    with rt.sessions() as session:
        current = session.scalar(
            select(Extraction.schema_version).where(
                Extraction.document_id == doc_id, Extraction.superseded_at.is_(None)
            )
        )
        page_nos = list(session.scalars(select(Page.page_no).where(Page.document_id == doc_id)))
    if current is not None and current >= schema_version:
        return {"document_id": document_id, "skipped": f"already at v{current}"}
    launch_extraction(doc_id, sorted(page_nos), schema_version)
    return {"document_id": document_id, "pages": len(page_nos)}
