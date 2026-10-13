"""The production adapters against real Postgres, MinIO and Redis (testcontainers)."""

import hashlib
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from alembic import command
from redis.asyncio import Redis

from manifest_api.adapters.idempotency import RedisIdempotencyStore
from manifest_api.adapters.settings import ApiSettings
from manifest_api.adapters.shipments import PendingPages, PostgresShipments
from manifest_api.adapters.uploads import S3Uploads
from manifest_api.errors import ConflictError, NotFoundError
from manifest_api.idempotency import StoredResponse
from manifest_api.rest.schemas import (
    CompletedPart,
    DocumentCreate,
    DocumentFinalize,
    DocumentUpload,
    ShipmentCreate,
)
from manifest_db.models import Shipment
from manifest_db.session import async_session_factory, sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.lifecycle import DocumentStatus, ShipmentStatus

pytestmark = pytest.mark.integration

MIB = 1024 * 1024


class Pipeline:
    def __init__(self) -> None:
        self.queued: list[tuple[UUID, UUID]] = []
        self.down = False

    async def enqueue(self, document_id: UUID, job_id: UUID) -> None:
        if self.down:
            raise ConnectionError("broker unreachable")
        self.queued.append((document_id, job_id))


@pytest.fixture
def database(postgres_url: str) -> Iterator[str]:
    with scratch_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
def settings(object_store: Any) -> ApiSettings:
    return ApiSettings(
        s3_endpoint_url=object_store.endpoint_url,
        s3_bucket=object_store.bucket,
        s3_access_key=object_store.access_key,
        s3_secret_key=object_store.secret_key,
        upload_part_size=5 * MIB,  # S3's minimum, so a 6 MiB file is two parts
    )


@pytest.fixture
async def service(
    database: str, settings: ApiSettings
) -> AsyncIterator[tuple[PostgresShipments, Pipeline]]:
    sessions = async_session_factory(database)
    pipeline = Pipeline()
    yield PostgresShipments(sessions, S3Uploads(settings), pipeline), pipeline
    await sessions.kw["bind"].dispose()


def pdf(size: int) -> DocumentCreate:
    return DocumentCreate(filename="doc.pdf", content_type="application/pdf", size_bytes=size)


async def upload(registered: DocumentUpload, data: bytes) -> DocumentFinalize:
    """PUT every part to its presigned URL, as a client would; the finalize body."""
    parts = []
    async with httpx.AsyncClient() as client:
        for part in registered.upload.parts:
            chunk = data[part.offset_bytes : part.offset_bytes + part.size_bytes]
            response = await client.put(part.url, content=chunk)
            response.raise_for_status()
            parts.append(CompletedPart(part_number=part.part_number, etag=response.headers["ETag"]))
    return DocumentFinalize(parts=parts)


async def test_a_document_goes_from_registration_to_the_queue(
    service: tuple[PostgresShipments, Pipeline],
) -> None:
    shipments, pipeline = service
    shipment = await shipments.create_shipment(ShipmentCreate(reference="PO-1"))
    assert shipment.status is ShipmentStatus.OPEN
    data = secrets.token_bytes(6 * MIB)
    request = pdf(len(data)).model_copy(update={"sha256": hashlib.sha256(data).hexdigest()})
    registered = await shipments.register_document(shipment.id, request)
    assert [p.size_bytes for p in registered.upload.parts] == [5 * MIB, 1 * MIB]
    assert registered.document.status is DocumentStatus.AWAITING_UPLOAD

    body = await upload(registered, data)
    accepted = await shipments.finalize_document(shipment.id, registered.document.id, body)
    assert accepted.status is DocumentStatus.QUEUED
    assert pipeline.queued == [(registered.document.id, accepted.job_id)]
    with pytest.raises(ConflictError, match="already queued"):
        await shipments.finalize_document(shipment.id, registered.document.id, body)

    detail = await shipments.get_shipment(shipment.id)
    assert detail is not None
    [document] = detail.documents
    assert (document.id, document.status, document.pages) == (
        registered.document.id,
        DocumentStatus.QUEUED,
        [],
    )
    listed = await shipments.list_shipments(limit=10, after=None)
    assert [s.id for s in listed] == [shipment.id]


async def test_a_lost_publish_puts_the_document_back_and_a_retry_queues_it(
    service: tuple[PostgresShipments, Pipeline],
) -> None:
    shipments, pipeline = service
    shipment = await shipments.create_shipment(ShipmentCreate())
    data = b"%PDF-1.4 tiny"
    registered = await shipments.register_document(shipment.id, pdf(len(data)))
    body = await upload(registered, data)
    pipeline.down = True
    with pytest.raises(ConnectionError):
        await shipments.finalize_document(shipment.id, registered.document.id, body)
    detail = await shipments.get_shipment(shipment.id)
    assert detail is not None
    assert detail.documents[0].status is DocumentStatus.AWAITING_UPLOAD
    pipeline.down = False
    # The upload completed the first time; the retry goes straight to queuing.
    accepted = await shipments.finalize_document(shipment.id, registered.document.id, body)
    assert accepted.status is DocumentStatus.QUEUED
    assert len(pipeline.queued) == 1


async def test_parts_that_dont_match_or_a_wrong_hash_are_conflicts(
    service: tuple[PostgresShipments, Pipeline],
) -> None:
    shipments, _ = service
    shipment = await shipments.create_shipment(ShipmentCreate())
    data = b"%PDF-1.4 packing list"
    lying = pdf(len(data)).model_copy(
        update={"sha256": hashlib.sha256(b"something else").hexdigest()}
    )
    registered = await shipments.register_document(shipment.id, lying)
    body = await upload(registered, data)
    wrong = DocumentFinalize(parts=[CompletedPart(part_number=1, etag='"0000"')])
    with pytest.raises(ConflictError, match="parts don't match"):
        await shipments.finalize_document(shipment.id, registered.document.id, wrong)
    with pytest.raises(ConflictError, match="sha256"):
        await shipments.finalize_document(shipment.id, registered.document.id, body)


async def test_unknown_or_closed_shipments_refuse_documents(
    service: tuple[PostgresShipments, Pipeline], database: str
) -> None:
    shipments, _ = service
    with pytest.raises(NotFoundError):
        await shipments.register_document(UUID(int=0), pdf(1))
    shipment = await shipments.create_shipment(ShipmentCreate())
    with sync_session_factory(database).begin() as session:
        row = session.get(Shipment, shipment.id)
        assert row is not None
        row.status = ShipmentStatus.PROCESSING
    with pytest.raises(ConflictError, match="no longer accepts"):
        await shipments.register_document(shipment.id, pdf(1))
    some_part = DocumentFinalize(parts=[CompletedPart(part_number=1, etag="x")])
    with pytest.raises(NotFoundError):
        await shipments.finalize_document(shipment.id, UUID(int=1), some_part)


async def test_admission_refuses_above_the_high_water_mark(
    service: tuple[PostgresShipments, Pipeline], database: str
) -> None:
    shipments, _ = service
    shipment = await shipments.create_shipment(ShipmentCreate())
    data = b"%PDF-1.4"
    for _ in range(2):
        registered = await shipments.register_document(shipment.id, pdf(len(data)))
        body = await upload(registered, data)
        await shipments.finalize_document(shipment.id, registered.document.id, body)
    sessions = async_session_factory(database)
    try:
        assert await PendingPages(sessions, high_water=3, retry_after=30).pending() == 2
        assert await PendingPages(sessions, high_water=3, retry_after=30).retry_after() is None
        assert await PendingPages(sessions, high_water=2, retry_after=30).retry_after() == 30
    finally:
        await sessions.kw["bind"].dispose()


@pytest.fixture
async def store(redis_url: str) -> AsyncIterator[RedisIdempotencyStore]:
    redis = Redis.from_url(redis_url)
    yield RedisIdempotencyStore(redis, ttl=60, namespace=f"test-{secrets.token_hex(4)}")
    await redis.aclose()


async def test_idempotency_keys_in_redis(store: RedisIdempotencyStore) -> None:
    assert await store.claim("k", "fp-1") is None  # claimed
    in_flight = await store.claim("k", "fp-1")
    assert in_flight is not None
    assert (in_flight.fingerprint, in_flight.response) == ("fp-1", None)
    response = StoredResponse(202, b'{"ok":true}', {"Location": "/v1/shipments/1"})
    await store.complete("k", response)
    replay = await store.claim("k", "fp-1")
    assert replay is not None
    assert replay.response == response
    reused = await store.claim("k", "fp-2")
    assert reused is not None
    assert reused.fingerprint == "fp-1"  # the caller answers 422
    assert await store.claim("released", "fp") is None
    await store.release("released")
    assert await store.claim("released", "fp") is None  # free again


async def test_a_simultaneous_burst_is_admitted_up_to_the_high_water_mark(
    database: str, settings: ApiSettings
) -> None:
    """Every finalize of a burst sees the same count before any commits; admission inside the
    queuing transaction, under a lock, still lets only high_water of them in."""
    import asyncio

    from manifest_api.errors import OverloadedError

    sessions = async_session_factory(database)
    pipeline = Pipeline()
    admission = PendingPages(sessions, high_water=2, retry_after=7)
    shipments = PostgresShipments(sessions, S3Uploads(settings), pipeline, admission)
    try:
        shipment = await shipments.create_shipment(ShipmentCreate())
        data = b"%PDF-1.4"
        bodies = []
        for _ in range(6):
            registered = await shipments.register_document(shipment.id, pdf(len(data)))
            bodies.append((registered.document.id, await upload(registered, data)))
        results = await asyncio.gather(
            *(shipments.finalize_document(shipment.id, d, body) for d, body in bodies),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, OverloadedError)]
        assert len(pipeline.queued) == 2
        assert len(refused) == 4
        assert {r.retry_after_seconds for r in refused} == {7}
        # Refused documents can try again: still awaiting upload, nothing queued for them.
        detail = await shipments.get_shipment(shipment.id)
        assert detail is not None
        statuses = sorted(str(d.status) for d in detail.documents)
        assert statuses == ["awaiting_upload"] * 4 + ["queued"] * 2
    finally:
        await sessions.kw["bind"].dispose()
