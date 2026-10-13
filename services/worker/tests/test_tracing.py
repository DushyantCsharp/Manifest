"""One trace from the finalize request to the last page: API, broker, chord, worker.

The real API adapters (Postgres, MinIO, Redis, RabbitMQ) and a real Celery worker run in this
process with in-memory telemetry. A client registers a document, uploads it through the
presigned URL and finalizes it; then the finalize request's server span must be the root of
the classify task, every page_extract task of the chord, each page's model call, and the
chord's reconcile callback.
"""

import io
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pypdfium2 as pdfium
import pytest
from alembic import command
from celery.contrib.testing.worker import start_worker
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

import manifest_telemetry
from manifest_api.adapters import pipeline as api_pipeline
from manifest_api.adapters.settings import ApiSettings
from manifest_api.adapters.wiring import build
from manifest_api.main import create_app
from manifest_db.models import Document
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION, PageFragment
from manifest_domain.lifecycle import DocumentStatus
from manifest_evals.golden import GOLDEN
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway
from manifest_llm.ratelimit import Limits, TokenBucket
from manifest_telemetry.testing import Capture, capture
from manifest_worker.app import celery_app
from manifest_worker.ports import ExtractedPage, PageInput
from manifest_worker.runtime import Runtime, use_runtime
from manifest_worker.storage import S3Storage
from manifest_worker.tasks.classify import classify
from manifest_worker.topology import EXCHANGE

PACKS = ("clean-0001", "clean-0002", "clean-0003")
LIMITS = Limits(requests_per_minute=1000, tokens_per_minute=1_000_000)


def test_the_api_publishes_where_the_worker_listens() -> None:
    route = celery_app.conf.task_routes[api_pipeline.CLASSIFY_TASK]
    assert route == {"queue": api_pipeline.CLASSIFY_ROUTING_KEY}
    assert EXCHANGE.name == api_pipeline.EXCHANGE
    assert classify.name == api_pipeline.CLASSIFY_TASK


def invoice_pages() -> bytes:
    """Three real invoice pages from the eval corpus, as one document."""
    merged = pdfium.PdfDocument.new()
    for pack in PACKS:
        merged.import_pages(pdfium.PdfDocument(GOLDEN / pack / "commercial_invoice.pdf"))
    buffer = io.BytesIO()
    merged.save(buffer)
    return buffer.getvalue()


class Model:
    """Answers every page with one valid line, through the real gateway (so: gen_ai spans)."""

    name = "scripted"

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        def read(value: str) -> dict[str, Any]:
            signals = {"self_report": "0.9", "evidence": "verified", "normaliser": "exact"}
            return {
                "value": value,
                "confidence": "0.9",
                "page": 1,
                "evidence": value,
                "signals": signals,
            }

        line = {"description": read("Item"), "quantity": read("1")}
        data = {"schema_version": CURRENT_SCHEMA_VERSION, "fields": {}, "line_items": [line]}
        return JsonResponse(data, 120, 60, finish_reason="end_turn")


class Extractor:
    def __init__(self, gateway: LlmGateway) -> None:
        self.gateway = gateway

    def extract(self, page: PageInput) -> ExtractedPage:
        result = self.gateway.structured(
            "scripted",
            "scripted-1",
            system="s",
            prompt=page.text,
            output=PageFragment,
            max_tokens=99,
        )
        return ExtractedPage(result.output, "scripted/scripted-1", 120, 60)


@pytest.fixture
def database(postgres_url: str) -> Iterator[str]:
    with scratch_database(postgres_url) as url:  # sync: it runs its own event loop
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
def worker(
    celery_broker: str, database: str, redis_url: str, object_store: Any
) -> Iterator[sessionmaker[Session]]:
    """A real worker (threads pool) on the test's broker, database and bucket."""
    capture()
    manifest_telemetry.instrument()
    redis = Redis.from_url(redis_url)
    bucket = TokenBucket(redis, namespace=f"manifest-test-{secrets.token_hex(4)}")
    gateway = LlmGateway({"scripted": Model()}, bucket, default_limits=LIMITS)
    storage = S3Storage(
        object_store.endpoint_url,
        object_store.bucket,
        object_store.access_key,
        object_store.secret_key,
        "us-east-1",
    )
    sessions = sync_session_factory(database)
    use_runtime(Runtime(sessions, storage, Extractor(gateway), hs_classifier=None))
    with start_worker(
        celery_app, pool="threads", concurrency=4, perform_ping_check=False, shutdown_timeout=30
    ):
        yield sessions
    use_runtime(None)


@pytest.fixture
async def client(
    worker: sessionmaker[Session],
    celery_broker: str,
    database: str,
    redis_url: str,
    object_store: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """The API with its production adapters, on the same broker, database and bucket."""
    settings = ApiSettings(
        broker_url=celery_broker,
        redis_url=redis_url,
        s3_endpoint_url=object_store.endpoint_url,
        s3_bucket=object_store.bucket,
        s3_access_key=object_store.access_key,
        s3_secret_key=object_store.secret_key,
    )
    ports, close = build(settings, database_url=database)
    app = create_app(ports)
    manifest_telemetry.instrument_fastapi(app)
    capture().clear_spans()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        yield http
    await close()


def _key() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def _submit(client: httpx.AsyncClient, pdf: bytes) -> UUID:
    """What a client does: shipment, document, presigned upload, finalize."""
    shipment = (await client.post("/v1/shipments", json={}, headers=_key())).json()
    registered = await client.post(
        f"/v1/shipments/{shipment['id']}/documents",
        json={"filename": "invoice.pdf", "content_type": "application/pdf", "size_bytes": len(pdf)},
        headers=_key(),
    )
    assert registered.status_code == 201, registered.text
    upload = registered.json()
    [part] = upload["upload"]["parts"]
    async with httpx.AsyncClient() as storage:  # straight to MinIO, as a browser would
        put = await storage.put(part["url"], content=pdf)
    put.raise_for_status()
    document_id = upload["document"]["id"]
    finalized = await client.post(
        f"/v1/shipments/{shipment['id']}/documents/{document_id}:finalize",
        json={"parts": [{"part_number": 1, "etag": put.headers["ETag"]}]},
        headers=_key(),
    )
    assert finalized.status_code == 202, finalized.text
    assert finalized.json()["status"] == "queued"
    return UUID(document_id)


def _ancestors(span: ReadableSpan, by_id: dict[int, ReadableSpan]) -> list[str]:
    names = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        found = by_id[parent.span_id]
        names.append(found.name)
        parent = found.parent
    return names


@pytest.mark.integration
async def test_one_trace_from_finalize_through_every_page(
    client: httpx.AsyncClient, worker: sessionmaker[Session], wait_for: Callable[..., None]
) -> None:
    telemetry: Capture = capture()
    document_id = await _submit(client, invoice_pages())

    def extracted() -> bool:
        with worker() as session:
            document = session.get(Document, document_id)
            return document is not None and document.status is DocumentStatus.EXTRACTED

    wait_for(extracted, timeout=60)
    wait_for(lambda: bool(telemetry.spans("run/manifest.reconcile")), timeout=30)

    [finalize] = [
        span
        for span in telemetry.spans()
        if span.kind is SpanKind.SERVER and ":finalize" in span.name
    ]
    trace_id = finalize.context.trace_id
    in_trace = [span for span in telemetry.spans() if span.context.trace_id == trace_id]
    names = [span.name for span in in_trace]
    assert names.count("apply_async/manifest.classify") == 1  # published by the API
    assert names.count("run/manifest.classify") == 1
    assert names.count("run/manifest.page_extract") == len(PACKS)  # the whole chord header
    assert names.count("run/manifest.reconcile") == 1  # and its callback
    assert names.count("chat scripted-1") == len(PACKS)  # each page's model call

    by_id = {span.context.span_id: span for span in in_trace}
    for call in (span for span in in_trace if span.name == "chat scripted-1"):
        lineage = _ancestors(call, by_id)
        assert lineage[0] == "run/manifest.page_extract"
        assert "run/manifest.classify" in lineage
        assert lineage[-1] == finalize.name  # the root is the finalize request
        assert dict(call.attributes or {})["gen_ai.response.finish_reasons"] == ("end_turn",)
    # The work in between is traced too: database statements, storage, the result backend.
    systems = {dict(span.attributes or {}).get("db.system") for span in in_trace}
    assert {"postgresql", "redis"} <= systems
    assert any(dict(s.attributes or {}).get("rpc.service") == "S3" for s in in_trace)
