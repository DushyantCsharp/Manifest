"""Queue ``page_extract``: one task per page — text layer, then structured extraction.

These tasks are a chord's header, so each one must *return* — even when its page fails — or
the chord never calls reconcile. Failures are recorded on the page and returned as data:

- transient (provider 429/5xx, S3 timeouts): retried with backoff + jitter up to max_retries,
  then the page fails and the message is parked in ``page_extract.dlq`` for redrive;
- rate limited by our shared bucket: re-queued with a countdown, not counted as a retry;
- invalid model output or unreadable page: no retry; the page fails and goes to review;
- rejected (bad credentials, extractor not wired) or a message that keeps killing workers:
  parked for redrive.

Results are upserted per (document, page, schema version): a rerun overwrites, never adds.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from manifest_db.models import Document, Page, PageExtraction
from manifest_domain.lifecycle import DocumentKind, PageStatus
from manifest_llm.errors import RateLimitedError
from manifest_worker.app import celery_app
from manifest_worker.errors import (
    CONTENT_ERRORS,
    REJECTED_ERRORS,
    TRANSIENT,
    ContentError,
    PoisonMessageError,
)
from manifest_worker.pipeline import POISON_AFTER, PipelineTask, defer, failed_deliveries, park
from manifest_worker.ports import ExtractedPage, PageInput
from manifest_worker.runtime import Runtime, runtime
from manifest_worker.storage import page_key
from manifest_worker.tasks.reconcile import reconcile
from manifest_worker.text import native_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageRef:
    document_id: UUID
    page_no: int
    schema_version: int


def _set_page(session: Session, ref: PageRef, **values: Any) -> None:
    session.execute(
        update(Page)
        .where(Page.document_id == ref.document_id, Page.page_no == ref.page_no)
        .values(**values, updated_at=func.now())
    )


def _set_result(session: Session, ref: PageRef, **values: Any) -> None:
    """Upsert this page's result for this schema version."""
    keys = {
        "document_id": ref.document_id,
        "page_no": ref.page_no,
        "schema_version": ref.schema_version,
    }
    statement = insert(PageExtraction).values(**keys, **values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=list(keys), set_={**values, "updated_at": func.now()}
        )
    )


def _start(rt: Runtime, ref: PageRef, attempt: int) -> tuple[str | None, DocumentKind | None]:
    with rt.sessions.begin() as session:
        _set_page(session, ref, status=PageStatus.PROCESSING, attempts=attempt, error=None)
        _set_result(
            session,
            ref,
            status=PageStatus.PROCESSING,
            fragment=None,
            failure=None,
            error=None,
            needs_review=False,
            attempts=attempt,
        )
        page = session.get(Page, (ref.document_id, ref.page_no))
        document = session.get(Document, ref.document_id)
        if page is None or document is None:
            raise ContentError(f"page {ref.page_no} of {ref.document_id} does not exist")
        return page.text, document.kind


def _read_text(rt: Runtime, ref: PageRef) -> str:
    """Native text layer, else OCR; stored on the page so later schema versions reuse it."""
    pdf = rt.storage.get(page_key(ref.document_id, ref.page_no))
    text = native_text(pdf)
    ocr_used = len(text.strip()) < rt.ocr_min_chars
    if ocr_used:
        text = rt.ocr(pdf)
    with rt.sessions.begin() as session:
        _set_page(session, ref, text=text, ocr_used=ocr_used)
    return text


def _note_error(rt: Runtime, ref: PageRef, exc: BaseException) -> None:
    with rt.sessions.begin() as session:
        _set_page(session, ref, error=f"{type(exc).__name__}: {exc}"[:2000])


def _fail(
    task: PipelineTask,
    rt: Runtime,
    ref: PageRef,
    exc: BaseException,
    *,
    failure: str,
    attempt: int,
    review: bool,
    to_dlq: bool,
) -> dict[str, Any]:
    message = f"{type(exc).__name__}: {exc}"[:2000]
    with rt.sessions.begin() as session:
        _set_page(session, ref, status=PageStatus.FAILED, error=message, attempts=attempt)
        _set_result(
            session,
            ref,
            status=PageStatus.FAILED,
            fragment=None,
            failure=failure,
            error=message,
            needs_review=review,
            attempts=attempt,
        )
    if to_dlq:
        park(task, exc, failure=failure)
    logger.warning("page %s of %s failed (%s): %s", ref.page_no, ref.document_id, failure, message)
    return {"page_no": ref.page_no, "status": "failed", "failure": failure}


def _succeed(rt: Runtime, ref: PageRef, extracted: ExtractedPage, attempt: int) -> None:
    with rt.sessions.begin() as session:
        _set_page(session, ref, status=PageStatus.EXTRACTED, error=None, attempts=attempt)
        _set_result(
            session,
            ref,
            status=PageStatus.EXTRACTED,
            fragment=extracted.fragment.model_dump(mode="json"),
            failure=None,
            error=None,
            needs_review=False,
            attempts=attempt,
            model=extracted.model,
            tokens_in=extracted.tokens_in,
            tokens_out=extracted.tokens_out,
        )


@celery_app.task(bind=True, base=PipelineTask, name="manifest.page_extract", stage="page_extract")
def page_extract(self: PipelineTask, document_id: str, page_no: int, schema_version: int) -> Any:
    rt = runtime()
    ref = PageRef(UUID(document_id), page_no, schema_version)
    attempt = self.request.retries + 1
    fail: dict[str, Any] = {"task": self, "rt": rt, "ref": ref, "attempt": attempt}

    if (crashes := failed_deliveries(self)) >= POISON_AFTER:
        exc = PoisonMessageError(f"delivered {crashes} times without completing")
        return _fail(**fail, exc=exc, failure="poison", review=False, to_dlq=True)
    try:
        text, kind = _start(rt, ref, attempt)
        if text is None:
            text = _read_text(rt, ref)
        extracted = rt.extractor.extract(
            PageInput(ref.document_id, page_no, kind, text, schema_version)
        )
        if extracted.fragment.schema_version != schema_version:
            raise ContentError(f"extractor answered in schema v{extracted.fragment.schema_version}")
    except RateLimitedError as exc:
        defer(self, exc.retry_after)
    except TRANSIENT as exc:
        if self.request.retries < self.max_retries:
            _note_error(rt, ref, exc)
            raise  # autoretry_for: backoff with jitter
        return _fail(**fail, exc=exc, failure="transient_exhausted", review=False, to_dlq=True)
    except CONTENT_ERRORS as exc:
        return _fail(**fail, exc=exc, failure="invalid_output", review=True, to_dlq=False)
    except REJECTED_ERRORS as exc:
        return _fail(**fail, exc=exc, failure="rejected", review=False, to_dlq=True)
    except Exception as exc:  # a bug must not stall the chord
        logger.exception("page %s of %s crashed", page_no, document_id)
        return _fail(**fail, exc=exc, failure="error", review=False, to_dlq=True)

    _succeed(rt, ref, extracted, attempt)
    if not self.request.chord:  # run on its own (e.g. redriven): refresh the document now
        reconcile.delay([], document_id, schema_version)
    return {"page_no": page_no, "status": "extracted"}
