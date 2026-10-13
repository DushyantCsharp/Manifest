"""The pipeline on real RabbitMQ, Redis, Postgres and MinIO: 2 of 10 pages fail.

Page 4's provider answers 503 every time (transient: retried 4 times, then parked in the DLQ);
page 8's provider returns output that fails validation (not retried: the shipment goes to
review). The other eight pages are extracted, after some are re-queued by the shared rate
limit. Then page 4 is redriven from the DLQ and the document completes without duplicates.
"""

import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlalchemy import func, select

from manifest_db.models import (
    Document,
    Extraction,
    HsSuggestion,
    LineItem,
    Page,
    PageExtraction,
    ReviewTask,
    Shipment,
)
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION, PageFragment
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, PageStatus
from manifest_domain.review import ReviewReason
from manifest_llm.errors import RateLimitedError, TransientProviderError
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway
from manifest_llm.ratelimit import Limits, TokenBucket
from manifest_worker import dlq
from manifest_worker.app import celery_app
from manifest_worker.ports import ExtractedPage, HsResult, LineToClassify, PageInput
from manifest_worker.runtime import Runtime, use_runtime
from manifest_worker.storage import S3Storage
from manifest_worker.tasks.classify import classify
from manifest_worker.tasks.page_extract import page_extract
from manifest_worker.topology import STAGES

WaitFor = Callable[..., None]

pytestmark = pytest.mark.integration

PAGES = 10
UNAVAILABLE, INVALID = 4, 8
LIMITS = Limits(requests_per_minute=30, tokens_per_minute=600_000)  # one request per 2 s


def text_pdf(texts: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page (a real text layer)."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = []
    for index, text in enumerate(texts):
        page, content = 4 + 2 * index, 5 + 2 * index
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objects[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        objects[page] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        ).encode()
        kids.append(f"{page} 0 R")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(texts)} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref, size = len(out), max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


class ScriptedProvider:
    """Answers like a model would, per page: 503s for one page, garbage for another."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: Counter[int] = Counter()
        self.unavailable = {UNAVAILABLE}

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        match = re.search(r"PAGE (\d+)", request.prompt)
        assert match is not None
        page = int(match.group(1))
        self.calls[page] += 1
        if page in self.unavailable:
            raise TransientProviderError("scripted 503: overloaded", status=503)
        quantity = "lots" if page == INVALID else str(page)  # page 8: fails validation

        def read(value: str, confidence: str) -> dict[str, Any]:
            signals = {"self_report": confidence, "evidence": "verified", "normaliser": "exact"}
            return {
                "value": value,
                "confidence": confidence,
                "page": page,
                "evidence": value,
                "signals": signals,
            }

        line = {
            "description": read(f"Item {page}", "0.95"),
            "quantity": read(quantity, "0.9"),
            "unit": read("pcs", "0.9"),
            "unit_price": read("10.00", "0.9"),
            "amount": read(f"{10 * page}.00", "0.9"),
            "country_of_origin": read("CN", "0.8"),
        }
        fields = {"currency": read("USD", "0.99")} if page == 1 else {}
        data = {"schema_version": CURRENT_SCHEMA_VERSION, "fields": fields, "line_items": [line]}
        return JsonResponse(data, input_tokens=120, output_tokens=60)


class ScriptedExtractor:
    """A page extractor that goes through the real gateway (rate limits, validation)."""

    def __init__(self, gateway: LlmGateway) -> None:
        self.gateway = gateway
        self.deferred: Counter[int] = Counter()

    def extract(self, page: PageInput) -> ExtractedPage:
        try:
            result = self.gateway.structured(
                "scripted",
                "scripted-1",
                system="Extract this page.",
                prompt=page.text,
                output=PageFragment,
                max_tokens=200,
            )
        except RateLimitedError:
            self.deferred[page.page_no] += 1
            raise
        model = f"{result.provider}/{result.model}"
        return ExtractedPage(result.output, model, result.input_tokens, result.output_tokens)


class FixedHsClassifier:
    def classify(self, line: LineToClassify) -> HsResult:
        return HsResult("847130", Decimal("0.9100"), "scripted", "scripted-hs", [])


@pytest.fixture
def pipeline(
    celery_broker: str, postgres_url: str, redis_url: str, object_store: Any
) -> Iterator[dict[str, Any]]:
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        namespace = f"manifest-test-{secrets.token_hex(4)}"
        redis = Redis.from_url(redis_url)
        bucket = TokenBucket(redis, namespace=namespace)
        provider = ScriptedProvider()
        gateway = LlmGateway(
            {"scripted": provider}, bucket, default_limits=LIMITS, max_inline_wait=0
        )
        extractor = ScriptedExtractor(gateway)
        storage = S3Storage(
            object_store.endpoint_url,
            object_store.bucket,
            object_store.access_key,
            object_store.secret_key,
            "us-east-1",
        )
        sessions = sync_session_factory(database)
        use_runtime(Runtime(sessions, storage, extractor, FixedHsClassifier()))
        page_extract.retry_backoff = 1  # keep the test quick: up to 1+2+4+8 s of backoff
        # Other workers have just spent this model's budget: the first calls must wait.
        while bucket.try_acquire("scripted", "scripted-1", LIMITS, 1) == 0:
            pass
        with start_worker(
            celery_app,
            pool="threads",
            concurrency=4,
            perform_ping_check=False,
            shutdown_timeout=30,
        ):
            yield {
                "sessions": sessions,
                "storage": storage,
                "provider": provider,
                "extractor": extractor,
                "vhost": celery_broker,
            }
        use_runtime(None)
        for key in redis.scan_iter(f"{namespace}:*"):
            redis.delete(key)


def add_document(pipeline: dict[str, Any]) -> UUID:
    key = f"tests/{uuid4()}.pdf"
    texts = [f"COMMERCIAL INVOICE PAGE {n}" for n in range(1, PAGES + 1)]
    pipeline["storage"].put(key, text_pdf(texts), "application/pdf")
    with pipeline["sessions"].begin() as session:
        shipment = Shipment(reference="PO-TEST")
        session.add(shipment)
        session.flush()
        document = Document(
            shipment_id=shipment.id,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=1,
            storage_key=key,
        )
        session.add(document)
        session.flush()
        return document.id


def counts(pipeline: dict[str, Any], document_id: UUID) -> dict[str, Any]:
    with pipeline["sessions"]() as session:
        document = session.get(Document, document_id)
        assert document is not None
        current = select(Extraction.id).where(Extraction.document_id == document_id)
        return {
            "status": document.status,
            "suggestions": session.scalar(
                select(func.count())
                .select_from(HsSuggestion)
                .join(LineItem, LineItem.id == HsSuggestion.line_item_id)
                .where(LineItem.extraction_id.in_(current))
            ),
        }


def test_two_failing_pages_of_ten(pipeline: dict[str, Any], wait_for: WaitFor) -> None:
    document_id = add_document(pipeline)
    classify.delay(str(document_id))
    wait_for(lambda: counts(pipeline, document_id)["suggestions"] == PAGES - 2)

    sessions, provider, extractor = (
        pipeline["sessions"],
        pipeline["provider"],
        pipeline["extractor"],
    )
    with sessions() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.status is DocumentStatus.PARTIALLY_EXTRACTED
        assert (document.kind, document.page_count) == (DocumentKind.COMMERCIAL_INVOICE, PAGES)
        pages = {
            p.page_no: p
            for p in session.scalars(select(Page).where(Page.document_id == document_id))
        }
        results = {
            r.page_no: r
            for r in session.scalars(
                select(PageExtraction).where(PageExtraction.document_id == document_id)
            )
        }
        extraction = session.scalars(
            select(Extraction).where(Extraction.document_id == document_id)
        ).one()
        lines = session.scalars(
            select(LineItem)
            .where(LineItem.extraction_id == extraction.id)
            .order_by(LineItem.line_no)
        ).all()
        review = session.scalars(
            select(ReviewTask).where(ReviewTask.shipment_id == document.shipment_id)
        ).one()

    # Final state: 8 pages extracted, the 2 failing ones recorded, one merged extraction.
    failed = {n for n, page in pages.items() if page.status is PageStatus.FAILED}
    assert failed == {UNAVAILABLE, INVALID}
    assert len(results) == PAGES
    assert results[UNAVAILABLE].failure == "transient_exhausted"
    assert not results[UNAVAILABLE].needs_review
    assert results[INVALID].failure == "invalid_output"
    assert results[INVALID].needs_review
    assert "503" in (pages[UNAVAILABLE].error or "")
    assert extraction.schema_version == CURRENT_SCHEMA_VERSION
    assert extraction.provenance is not None
    expected = [n for n in range(1, PAGES + 1) if n not in failed]
    assert [line.description for line in lines] == [f"Item {n}" for n in expected]
    assert [line.line_no for line in lines] == list(range(1, len(expected) + 1))
    assert all(line.currency == "USD" and line.country_of_origin == "CN" for line in lines)
    assert ReviewReason.PARTIAL_EXTRACTION in review.reasons

    # Retry counts: page 4 tried 1 + 4 retries; page 8 never retried; rate-limit re-queues
    # happened but cost no retries and never reached the provider.
    assert pages[UNAVAILABLE].attempts == 5
    assert provider.calls[UNAVAILABLE] == 5
    assert pages[INVALID].attempts == 1
    assert provider.calls[INVALID] == 1
    assert all(provider.calls[n] == 1 and pages[n].attempts == 1 for n in expected)
    assert sum(extractor.deferred.values()) >= 1

    # DLQ: exactly page 4's message, parked with why; validation failures don't go there.
    with dlq.Connection(pipeline["vhost"]) as connection, dlq.dead_letters(connection) as letters:
        assert [(letter.stage, letter.task) for letter in letters] == [
            ("page_extract", "manifest.page_extract")
        ]
        [letter] = letters
        assert letter.body[0] == [str(document_id), UNAVAILABLE, CURRENT_SCHEMA_VERSION]
        assert letter.failure == "transient_exhausted"
        assert letter.exception == "TransientProviderError"
        assert letter.headers["x-manifest-retries"] == 4

        # The provider recovers; redrive page 4 alone.
        provider.unavailable.clear()
        dlq.redrive(connection, letters)

    wait_for(lambda: counts(pipeline, document_id)["suggestions"] == PAGES - 1)
    assert (
        counts(pipeline, document_id)["status"] is DocumentStatus.PARTIALLY_EXTRACTED
    )  # 8 still bad

    with sessions() as session:
        page_four = session.get(Page, (document_id, UNAVAILABLE))
        assert page_four is not None
        assert page_four.status is PageStatus.EXTRACTED
        assert page_four.attempts == 1  # a redrive starts a fresh retry budget
        assert session.scalar(select(func.count()).select_from(PageExtraction)) == PAGES
        assert session.scalar(select(func.count()).select_from(Extraction)) == 1  # overwritten
        assert session.scalar(select(func.count()).select_from(LineItem)) == PAGES - 1
    with dlq.Connection(pipeline["vhost"]) as connection, dlq.dead_letters(connection) as letters:
        assert letters == []
    assert set(STAGES) >= {"page_extract"}
