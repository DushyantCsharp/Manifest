"""What the API needs from the rest of the system.

Handlers and resolvers depend only on these Protocols. Adapters (Postgres, S3, the broker,
Redis) implement them; a port without an adapter is *unwired* and its calls fail with
``NotWiredError`` (REST 501). Tests pass in-memory fakes to ``create_app``.
"""

from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Annotated, NoReturn, Protocol, Self, cast
from uuid import UUID

from fastapi import Depends, Request

from manifest_api.errors import NotWiredError
from manifest_api.graphql.types import ExtractedField, FieldValueType, ReviewTask
from manifest_api.idempotency import IdempotencyStore
from manifest_api.pagination import ReviewTaskCursor, ShipmentCursor
from manifest_api.rest.schemas import (
    DocumentCreate,
    DocumentFinalize,
    DocumentUpload,
    FinalizeAccepted,
    Shipment,
    ShipmentCreate,
    ShipmentDetail,
)
from manifest_domain.review import ReviewPriority, ReviewTaskStatus


class ShipmentService(Protocol):
    async def create_shipment(self, request: ShipmentCreate) -> Shipment: ...

    async def list_shipments(
        self, *, limit: int, after: ShipmentCursor | None
    ) -> Sequence[Shipment]:
        """At most ``limit`` shipments after ``after``, ordered (created_at, id) descending."""
        ...

    async def get_shipment(self, shipment_id: UUID) -> ShipmentDetail | None: ...

    async def register_document(self, shipment_id: UUID, request: DocumentCreate) -> DocumentUpload:
        """Create the document and start a presigned multipart upload.

        Raises ``NotFoundError`` (no such shipment) or ``ConflictError`` (shipment closed).
        """
        ...

    async def finalize_document(
        self, shipment_id: UUID, document_id: UUID, request: DocumentFinalize
    ) -> FinalizeAccepted:
        """Complete the multipart upload and enqueue the pipeline.

        Raises ``NotFoundError`` or ``ConflictError`` (already finalized, parts don't match).
        """
        ...


class AdmissionControl(Protocol):
    async def retry_after(self) -> int | None:
        """Seconds to wait when pending pages are above the high-water mark, else None."""
        ...


class ReviewService(Protocol):
    async def list_tasks(
        self,
        *,
        status: Collection[ReviewTaskStatus] | None,
        priority: Collection[ReviewPriority] | None,
        first: int,
        after: ReviewTaskCursor | None,
    ) -> Sequence[ReviewTask]:
        """At most ``first`` tasks after ``after``, ordered (priority, created_at, id)."""
        ...

    async def get_task(self, task_id: str) -> ReviewTask | None: ...

    async def approve_field(self, field_id: str) -> ExtractedField:
        """Raises ``NotFoundError`` or ``ConflictError`` (task already approved)."""
        ...

    async def correct_field(
        self, field_id: str, value: FieldValueType, reason: str
    ) -> ExtractedField:
        """Raises ``NotFoundError``, ``ConflictError`` or ``InvalidValueError`` (wrong type)."""
        ...

    async def approve_task(self, task_id: str) -> ReviewTask:
        """Raises ``NotFoundError`` or ``ConflictError`` (already approved)."""
        ...


class _Unwired:
    def __init__(self, port: str) -> None:
        self._port = port

    def __getattr__(self, name: str) -> Callable[..., Awaitable[NoReturn]]:
        async def call(*_args: object, **_kwargs: object) -> NoReturn:
            raise NotWiredError(f"{self._port}.{name} has no adapter yet")

        return call


@dataclass(frozen=True)
class Ports:
    shipments: ShipmentService
    admission: AdmissionControl
    idempotency: IdempotencyStore
    review: ReviewService

    @classmethod
    def unwired(cls) -> Self:
        return cls(
            shipments=cast(ShipmentService, _Unwired("shipments")),
            admission=cast(AdmissionControl, _Unwired("admission")),
            idempotency=cast(IdempotencyStore, _Unwired("idempotency")),
            review=cast(ReviewService, _Unwired("review")),
        )


def get_ports(request: Request) -> Ports:
    return cast(Ports, request.app.state.ports)


PortsDep = Annotated[Ports, Depends(get_ports)]
