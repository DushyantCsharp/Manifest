"""Shipments and documents in Postgres, uploads in S3, processing on the broker.

Finalizing completes the upload, then moves the document from ``awaiting_upload`` to
``queued`` with a conditional update (of two concurrent finalizes, one wins), commits, and
only then publishes ``classify``, so the worker never sees a document the database hasn't
queued. If publishing fails the document goes back to ``awaiting_upload``; the upload is
already complete, so the client's retry skips straight to queuing.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from manifest_api.adapters.uploads import S3Uploads
from manifest_api.errors import ConflictError, NotFoundError
from manifest_api.pagination import ShipmentCursor
from manifest_api.rest.schemas import (
    Document,
    DocumentCreate,
    DocumentFinalize,
    DocumentPage,
    DocumentUpload,
    FinalizeAccepted,
    MultipartUpload,
    Shipment,
    ShipmentCreate,
    ShipmentDetail,
    UploadContentType,
    UploadPart,
)
from manifest_db import models
from manifest_db.keyset import shipments_page
from manifest_domain.lifecycle import DocumentStatus, PageStatus, ShipmentStatus, TextSource


class Pipeline(Protocol):
    async def enqueue(self, document_id: UUID, job_id: UUID) -> None: ...


def storage_key(shipment_id: UUID, document_id: UUID) -> str:
    return f"documents/{shipment_id}/{document_id}"


def _shipment(row: models.Shipment) -> Shipment:
    return Shipment(
        id=row.id,
        reference=row.reference,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _page(row: models.Page) -> DocumentPage:
    source = None
    if row.ocr_used is not None:
        source = TextSource.OCR if row.ocr_used else TextSource.NATIVE
    return DocumentPage(
        page_number=row.page_no,
        status=row.status,
        text_source=source,
        attempts=row.attempts,
        error=row.error if row.status is PageStatus.FAILED else None,
    )


def _document(
    row: models.Document, pages: Sequence[models.Page], schema_version: int | None
) -> Document:
    return Document(
        id=row.id,
        filename=row.filename,
        content_type=UploadContentType(row.content_type),
        size_bytes=row.size_bytes,
        kind=row.kind,
        status=row.status,
        page_count=row.page_count,
        extraction_schema_version=schema_version,
        pages=[_page(page) for page in pages],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresShipments:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], uploads: S3Uploads, pipeline: Pipeline
    ) -> None:
        self._sessions = sessions
        self._uploads = uploads
        self._pipeline = pipeline

    async def create_shipment(self, request: ShipmentCreate) -> Shipment:
        async with self._sessions.begin() as session:
            row = models.Shipment(reference=request.reference, status=ShipmentStatus.OPEN)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _shipment(row)

    async def list_shipments(
        self, *, limit: int, after: ShipmentCursor | None
    ) -> Sequence[Shipment]:
        key = (after.created_at, after.id) if after is not None else None
        async with self._sessions() as session:
            rows = (await session.scalars(shipments_page(limit, key))).all()
            return [_shipment(row) for row in rows]

    async def get_shipment(self, shipment_id: UUID) -> ShipmentDetail | None:
        async with self._sessions() as session:
            shipment = await session.get(models.Shipment, shipment_id)
            if shipment is None:
                return None
            documents = (
                await session.scalars(
                    select(models.Document)
                    .where(models.Document.shipment_id == shipment_id)
                    .order_by(models.Document.created_at, models.Document.id)
                )
            ).all()
            ids = [document.id for document in documents]
            pages: dict[UUID, list[models.Page]] = {i: [] for i in ids}
            versions: dict[UUID, int] = {}
            if ids:
                for page in await session.scalars(
                    select(models.Page)
                    .where(models.Page.document_id.in_(ids))
                    .order_by(models.Page.document_id, models.Page.page_no)
                ):
                    pages[page.document_id].append(page)
                for document_id, version in await session.execute(
                    select(models.Extraction.document_id, models.Extraction.schema_version).where(
                        models.Extraction.document_id.in_(ids),
                        models.Extraction.superseded_at.is_(None),
                    )
                ):
                    versions[document_id] = version
            return ShipmentDetail(
                **_shipment(shipment).model_dump(),
                documents=[_document(d, pages[d.id], versions.get(d.id)) for d in documents],
            )

    async def register_document(self, shipment_id: UUID, request: DocumentCreate) -> DocumentUpload:
        async with self._sessions() as session:
            await self._accepting(session, shipment_id)
        document_id = uuid4()
        key = storage_key(shipment_id, document_id)
        # Outside the transaction: S3 is slow next to a row lock. An upload whose document
        # never commits is left incomplete; MinIO aborts those after a day (on AWS S3, give
        # the bucket an AbortIncompleteMultipartUpload lifecycle rule).
        started = await self._uploads.start(key, request.size_bytes, request.content_type)
        async with self._sessions.begin() as session:
            await self._accepting(session, shipment_id, lock=True)
            row = models.Document(
                id=document_id,
                shipment_id=shipment_id,
                filename=request.filename,
                content_type=str(request.content_type),
                size_bytes=request.size_bytes,
                sha256=request.sha256,
                storage_key=key,
                status=DocumentStatus.AWAITING_UPLOAD,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            document = _document(row, [], None)
        upload = MultipartUpload(
            upload_id=started.upload_id,
            part_size_bytes=started.part_size,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._uploads.url_ttl),
            parts=[
                UploadPart(
                    part_number=part.number,
                    url=part.url,
                    offset_bytes=part.offset,
                    size_bytes=part.size,
                )
                for part in started.parts
            ],
        )
        return DocumentUpload(document=document, upload=upload)

    async def finalize_document(
        self, shipment_id: UUID, document_id: UUID, request: DocumentFinalize
    ) -> FinalizeAccepted:
        async with self._sessions() as session:
            document = await session.get(models.Document, document_id)
            if document is None or document.shipment_id != shipment_id:
                raise NotFoundError(f"document {document_id} does not exist")
            if document.status is not DocumentStatus.AWAITING_UPLOAD:
                raise ConflictError(f"document {document_id} is already {document.status}")
            key, size, sha256 = document.storage_key, document.size_bytes, document.sha256
        parts = [(part.part_number, part.etag) for part in request.parts]
        await self._uploads.complete(key, parts, size)
        if sha256 is not None and await self._uploads.sha256(key) != sha256:
            raise ConflictError("the uploaded file doesn't match the registered sha256")

        async with self._sessions.begin() as session:
            queued = await session.execute(
                update(models.Document)
                .where(
                    models.Document.id == document_id,
                    models.Document.status == DocumentStatus.AWAITING_UPLOAD,
                )
                .values(status=DocumentStatus.QUEUED, updated_at=func.now())
            )
            if queued.rowcount == 0:  # type: ignore[attr-defined]
                raise ConflictError(f"document {document_id} was finalized meanwhile")
        job_id = uuid4()
        try:
            await self._pipeline.enqueue(document_id, job_id)
        except Exception:
            async with self._sessions.begin() as session:
                await session.execute(
                    update(models.Document)
                    .where(
                        models.Document.id == document_id,
                        models.Document.status == DocumentStatus.QUEUED,
                    )
                    .values(status=DocumentStatus.AWAITING_UPLOAD, updated_at=func.now())
                )
            raise
        return FinalizeAccepted(
            job_id=job_id, document_id=document_id, status=DocumentStatus.QUEUED
        )

    @staticmethod
    async def _accepting(session: AsyncSession, shipment_id: UUID, *, lock: bool = False) -> None:
        shipment = await session.get(models.Shipment, shipment_id, with_for_update=lock)
        if shipment is None:
            raise NotFoundError(f"shipment {shipment_id} does not exist")
        if shipment.status is not ShipmentStatus.OPEN:
            raise ConflictError(f"shipment {shipment_id} no longer accepts documents")


class PendingPages:
    """Admission control: refuse new documents while too many pages wait or run.

    A queued document not yet split into pages counts as one page.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, high_water: int, retry_after: int
    ) -> None:
        self._sessions = sessions
        self._high_water, self._retry_after = high_water, retry_after

    async def pending(self) -> int:
        async with self._sessions() as session:
            pages = await session.scalar(
                select(func.count())
                .select_from(models.Page)
                .where(models.Page.status.in_((PageStatus.PENDING, PageStatus.PROCESSING)))
            )
            unsplit = await session.scalar(
                select(func.count())
                .select_from(models.Document)
                .where(
                    models.Document.status.in_((DocumentStatus.QUEUED, DocumentStatus.PROCESSING)),
                    models.Document.page_count.is_(None),
                )
            )
        return int(pages or 0) + int(unsplit or 0)

    async def retry_after(self) -> int | None:
        return self._retry_after if await self.pending() >= self._high_water else None
