"""In-memory fakes for the API's ports, and an app wired to them.

The fakes only model what the API layer relies on (ordering, not-found, conflicts); real
adapters are tested against real infrastructure.
"""

import asyncio
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import strawberry
from fastapi import FastAPI

from manifest_api.errors import ConflictError, InvalidValueError, NotFoundError
from manifest_api.graphql.types import (
    BoundingBox,
    DateValue,
    Discrepancy,
    DiscrepancyObservation,
    ExtractedField,
    Extraction,
    FieldCorrection,
    FieldValueType,
    HsCandidate,
    HsSuggestion,
    LineItem,
    MoneyValue,
    QuantityValue,
    ReviewTask,
    TextValue,
)
from manifest_api.idempotency import IdempotencyRecord, StoredResponse
from manifest_api.main import create_app
from manifest_api.pagination import ReviewTaskCursor, ShipmentCursor
from manifest_api.ports import Ports
from manifest_api.rest.schemas import (
    Document,
    DocumentCreate,
    DocumentFinalize,
    DocumentUpload,
    FinalizeAccepted,
    MultipartUpload,
    Shipment,
    ShipmentCreate,
    ShipmentDetail,
    UploadPart,
)
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, ShipmentStatus
from manifest_domain.review import (
    DiscrepancyKind,
    DiscrepancySeverity,
    FieldStatus,
    ReviewPriority,
    ReviewReason,
    ReviewTaskStatus,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PART_SIZE = 8 * 1024 * 1024


def new_id() -> strawberry.ID:
    return strawberry.ID(str(uuid4()))


# ---------------------------------------------------------------- REST ports


class FakeShipments:
    def __init__(self) -> None:
        self.shipments: dict[UUID, ShipmentDetail] = {}
        self.now = T0
        self.finalized = 0
        # Set to hold create_shipment open, to observe a request while it is in flight.
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    def _tick(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    async def create_shipment(self, request: ShipmentCreate) -> Shipment:
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        now = self._tick()
        detail = ShipmentDetail(
            id=uuid4(),
            reference=request.reference,
            status=ShipmentStatus.OPEN,
            created_at=now,
            updated_at=now,
            documents=[],
        )
        self.shipments[detail.id] = detail
        return Shipment.model_validate(detail.model_dump(exclude={"documents"}))

    async def list_shipments(self, *, limit: int, after: ShipmentCursor | None) -> list[Shipment]:
        rows = sorted(self.shipments.values(), key=lambda s: (s.created_at, s.id), reverse=True)
        if after is not None:
            rows = [s for s in rows if (s.created_at, s.id) < (after.created_at, after.id)]
        return [Shipment.model_validate(s.model_dump(exclude={"documents"})) for s in rows[:limit]]

    async def get_shipment(self, shipment_id: UUID) -> ShipmentDetail | None:
        return self.shipments.get(shipment_id)

    async def register_document(self, shipment_id: UUID, request: DocumentCreate) -> DocumentUpload:
        shipment = self.shipments.get(shipment_id)
        if shipment is None:
            raise NotFoundError(f"shipment {shipment_id} does not exist")
        if shipment.status is not ShipmentStatus.OPEN:
            raise ConflictError(f"shipment {shipment_id} no longer accepts documents")
        now = self._tick()
        document = Document(
            id=uuid4(),
            filename=request.filename,
            content_type=request.content_type,
            size_bytes=request.size_bytes,
            kind=None,
            status=DocumentStatus.AWAITING_UPLOAD,
            page_count=None,
            extraction_schema_version=None,
            pages=[],
            created_at=now,
            updated_at=now,
        )
        shipment.documents.append(document)
        parts = [
            UploadPart(
                part_number=number,
                url=f"http://storage.test/{document.id}?partNumber={number}",
                offset_bytes=offset,
                size_bytes=min(PART_SIZE, request.size_bytes - offset),
            )
            for number, offset in enumerate(range(0, request.size_bytes, PART_SIZE), start=1)
        ]
        upload = MultipartUpload(
            upload_id=f"upload-{document.id}",
            part_size_bytes=PART_SIZE,
            expires_at=now + timedelta(hours=1),
            parts=parts,
        )
        return DocumentUpload(document=document, upload=upload)

    async def finalize_document(
        self, shipment_id: UUID, document_id: UUID, request: DocumentFinalize
    ) -> FinalizeAccepted:
        shipment = self.shipments.get(shipment_id)
        documents = shipment.documents if shipment else []
        document = next((d for d in documents if d.id == document_id), None)
        if document is None:
            raise NotFoundError(f"document {document_id} does not exist")
        if document.status is not DocumentStatus.AWAITING_UPLOAD:
            raise ConflictError(f"document {document_id} is already {document.status}")
        document.status = DocumentStatus.QUEUED
        self.finalized += 1
        return FinalizeAccepted(job_id=uuid4(), document_id=document_id, status=document.status)


@dataclass
class FakeAdmission:
    retry_after_seconds: int | None = None

    async def retry_after(self) -> int | None:
        return self.retry_after_seconds


@dataclass
class InMemoryIdempotencyStore:
    records: dict[str, IdempotencyRecord] = field(default_factory=dict)

    async def claim(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        if key in self.records:
            return self.records[key]
        self.records[key] = IdempotencyRecord(fingerprint=fingerprint, response=None)
        return None

    async def complete(self, key: str, response: StoredResponse) -> None:
        self.records[key] = replace(self.records[key], response=response)

    async def release(self, key: str) -> None:
        self.records.pop(key, None)


# ---------------------------------------------------------------- review port


def _field(
    key: str,
    value: FieldValueType | None,
    confidence: float,
    *,
    page: int = 1,
    status: FieldStatus = FieldStatus.EXTRACTED,
) -> ExtractedField:
    return ExtractedField(
        id=new_id(),
        key=key,
        value=value,
        confidence=confidence,
        source_page=page if value is not None else None,
        bbox=BoundingBox(left=0.1, top=0.2, right=0.4, bottom=0.25) if value is not None else None,
        status=status,
        correction=None,
    )


def make_task(
    priority: ReviewPriority,
    created_at: datetime,
    status: ReviewTaskStatus = ReviewTaskStatus.OPEN,
) -> ReviewTask:
    invoice_id, packing_list_id = new_id(), new_id()
    invoice = Extraction(
        id=new_id(),
        document_id=invoice_id,
        document_kind=DocumentKind.COMMERCIAL_INVOICE,
        schema_version=1,
        fields=[
            _field("invoice_number", TextValue(text="INV-1001"), 0.98),
            _field("invoice_date", DateValue(date=date(2026, 9, 30)), 0.95),
            _field("invoice_total", MoneyValue(amount=Decimal("1250.00"), currency="USD"), 0.62),
            _field("gross_weight", QuantityValue(amount=Decimal("120.5"), unit="kg"), 0.71),
            _field("incoterm", None, 0.0, status=FieldStatus.MISSING),
        ],
        line_items=[
            LineItem(
                line_number=1,
                fields=[_field("description", TextValue(text="Laptop computers"), 0.97)],
                hs_suggestion=HsSuggestion(
                    code="847130",
                    description="Portable automatic data processing machines, <= 10 kg",
                    confidence=0.88,
                    rationale="Line describes laptops; 8471.30 covers portable ADP machines.",
                    retrieved_candidates=[
                        HsCandidate(code="847130", description="Portable ADP machines", score=0.91),
                        HsCandidate(code="847141", description="Other ADP machines", score=0.64),
                    ],
                ),
            )
        ],
    )
    weight_mismatch = Discrepancy(
        id=new_id(),
        kind=DiscrepancyKind.VALUE_MISMATCH,
        severity=DiscrepancySeverity.WARNING,
        field_key="gross_weight",
        line_number=None,
        message="Invoice and packing list disagree on gross weight.",
        observations=[
            DiscrepancyObservation(
                document_id=invoice_id,
                document_kind=DocumentKind.COMMERCIAL_INVOICE,
                value=QuantityValue(amount=Decimal("120.5"), unit="kg"),
                source_page=1,
                bbox=BoundingBox(left=0.6, top=0.7, right=0.8, bottom=0.72),
            ),
            DiscrepancyObservation(
                document_id=packing_list_id,
                document_kind=DocumentKind.PACKING_LIST,
                value=QuantityValue(amount=Decimal("125"), unit="kg"),
                source_page=2,
                bbox=None,
            ),
        ],
    )
    return ReviewTask(
        id=new_id(),
        shipment_id=new_id(),
        status=status,
        priority=priority,
        reasons=[ReviewReason.LOW_CONFIDENCE, ReviewReason.DISCREPANCY],
        created_at=created_at,
        extractions=[invoice],
        discrepancies=[weight_mismatch],
    )


def _queue_key(task: ReviewTask) -> tuple[int, datetime, UUID]:
    return (int(task.priority), task.created_at, UUID(str(task.id)))


class FakeReview:
    def __init__(self, tasks: Sequence[ReviewTask]) -> None:
        self.tasks = {str(task.id): task for task in tasks}

    def queue(self) -> list[ReviewTask]:
        return sorted(self.tasks.values(), key=_queue_key)

    def find_field(self, key: str, task: ReviewTask) -> ExtractedField:
        return next(f for e in task.extractions for f in e.fields if f.key == key)

    def _locate(self, field_id: str) -> tuple[ReviewTask, ExtractedField]:
        for task in self.tasks.values():
            for extraction in task.extractions:
                lines = (f for line in extraction.line_items for f in line.fields)
                for candidate in (*extraction.fields, *lines):
                    if str(candidate.id) == field_id:
                        return task, candidate
        raise NotFoundError(f"field {field_id} does not exist")

    @staticmethod
    def _ensure_open(task: ReviewTask) -> None:
        if task.status is ReviewTaskStatus.APPROVED:
            raise ConflictError(f"task {task.id} is already approved")

    async def list_tasks(
        self,
        *,
        status: Collection[ReviewTaskStatus] | None,
        priority: Collection[ReviewPriority] | None,
        first: int,
        after: ReviewTaskCursor | None,
    ) -> list[ReviewTask]:
        rows = self.queue()
        if status:
            rows = [t for t in rows if t.status in status]
        if priority:
            rows = [t for t in rows if t.priority in priority]
        if after is not None:
            rows = [t for t in rows if _queue_key(t) > (after.priority, after.created_at, after.id)]
        return rows[:first]

    async def get_task(self, task_id: str) -> ReviewTask | None:
        return self.tasks.get(task_id)

    async def approve_field(self, field_id: str) -> ExtractedField:
        task, target = self._locate(field_id)
        self._ensure_open(task)
        target.status = FieldStatus.APPROVED
        task.status = ReviewTaskStatus.IN_PROGRESS
        return target

    async def correct_field(
        self, field_id: str, value: FieldValueType, reason: str
    ) -> ExtractedField:
        task, target = self._locate(field_id)
        self._ensure_open(task)
        if target.value is not None and type(value) is not type(target.value):
            expected, given = type(target.value).__name__, type(value).__name__
            raise InvalidValueError(f"{target.key} takes a {expected}, not a {given}")
        target.correction = FieldCorrection(
            previous_value=target.value, reason=reason, corrected_at=datetime.now(UTC)
        )
        target.value = value
        target.status = FieldStatus.CORRECTED
        task.status = ReviewTaskStatus.IN_PROGRESS
        return target

    async def approve_task(self, task_id: str) -> ReviewTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task {task_id} does not exist")
        self._ensure_open(task)
        task.status = ReviewTaskStatus.APPROVED
        return task


# ---------------------------------------------------------------- fixtures


@dataclass
class Fakes:
    shipments: FakeShipments
    admission: FakeAdmission
    idempotency: InMemoryIdempotencyStore
    review: FakeReview


@pytest.fixture
def fakes() -> Fakes:
    hour = timedelta(hours=1)
    tasks = [
        make_task(ReviewPriority.URGENT, T0 + 3 * hour),
        make_task(ReviewPriority.URGENT, T0 + hour),
        make_task(ReviewPriority.HIGH, T0),
        make_task(ReviewPriority.NORMAL, T0, ReviewTaskStatus.IN_PROGRESS),
        make_task(ReviewPriority.LOW, T0, ReviewTaskStatus.APPROVED),
    ]
    return Fakes(FakeShipments(), FakeAdmission(), InMemoryIdempotencyStore(), FakeReview(tasks))


@pytest.fixture
def app(fakes: Fakes) -> FastAPI:
    ports = Ports(
        shipments=fakes.shipments,
        admission=fakes.admission,
        idempotency=fakes.idempotency,
        review=fakes.review,
    )
    return create_app(ports)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
