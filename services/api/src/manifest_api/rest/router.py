from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from manifest_api.errors import NotFoundError, OverloadedError
from manifest_api.idempotency import (
    IDEMPOTENT_REPLAYED_HEADER,
    IdempotencyKey,
    StoredResponse,
    run_idempotent,
)
from manifest_api.pagination import ShipmentCursor
from manifest_api.ports import PortsDep
from manifest_api.problems import problem_responses
from manifest_api.rest.schemas import (
    DocumentCreate,
    DocumentFinalize,
    DocumentUpload,
    FinalizeAccepted,
    Shipment,
    ShipmentCreate,
    ShipmentDetail,
    ShipmentPage,
)

router = APIRouter(prefix="/v1")

_REPLAYED = {
    IDEMPOTENT_REPLAYED_HEADER: {
        "description": "`true` when this is the stored response to an earlier identical request.",
        "schema": {"type": "string", "enum": ["true"]},
    }
}


def _created(description: str) -> dict[int | str, dict[str, object]]:
    return {
        201: {
            "description": description,
            "headers": {
                "Location": {
                    "description": "URL of the new resource.",
                    "schema": {"type": "string"},
                },
                **_REPLAYED,
            },
        }
    }


_IDEMPOTENCY_PROBLEMS = {
    400: "`Idempotency-Key` header missing (`idempotency-key-missing`).",
    409: "Another request with this `Idempotency-Key` is still running.",
    422: "Body failed validation, or the `Idempotency-Key` was used for a different request.",
}


@router.post(
    "/shipments",
    tags=["shipments"],
    status_code=201,
    response_model=Shipment,
    responses={**_created("Shipment created."), **problem_responses(_IDEMPOTENCY_PROBLEMS)},
)
async def create_shipment(
    body: ShipmentCreate, request: Request, idempotency_key: IdempotencyKey, ports: PortsDep
) -> Response:
    """Create an empty shipment to attach documents to."""

    async def handle() -> StoredResponse:
        shipment = await ports.shipments.create_shipment(body)
        return StoredResponse.json(201, shipment, {"Location": f"/v1/shipments/{shipment.id}"})

    return await run_idempotent(ports.idempotency, idempotency_key, request, handle)


@router.get(
    "/shipments",
    tags=["shipments"],
    response_model=ShipmentPage,
    responses=problem_responses(
        {
            400: "`cursor` is malformed or from another listing (`invalid-cursor`).",
            422: "`limit` out of range.",
        }
    ),
)
async def list_shipments(
    ports: PortsDep,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 25,
    cursor: Annotated[
        str | None,
        Query(max_length=512, description="`next_cursor` from the previous page."),
    ] = None,
) -> ShipmentPage:
    """List shipments, newest first, with keyset pagination."""
    after = ShipmentCursor.decode(cursor) if cursor is not None else None
    # One extra row tells us whether another page exists without a COUNT.
    rows = await ports.shipments.list_shipments(limit=limit + 1, after=after)
    items = list(rows[:limit])
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = ShipmentCursor(last.created_at, last.id).encode()
    return ShipmentPage(items=items, next_cursor=next_cursor)


@router.get(
    "/shipments/{shipment_id}",
    tags=["shipments"],
    response_model=ShipmentDetail,
    responses=problem_responses({404: "No such shipment.", 422: "`shipment_id` is not a UUID."}),
)
async def get_shipment(shipment_id: UUID, ports: PortsDep) -> ShipmentDetail:
    """A shipment with the processing status of each document and each page."""
    shipment = await ports.shipments.get_shipment(shipment_id)
    if shipment is None:
        raise NotFoundError(f"shipment {shipment_id} does not exist")
    return shipment


@router.post(
    "/shipments/{shipment_id}/documents",
    tags=["documents"],
    status_code=201,
    response_model=DocumentUpload,
    responses={
        **_created("Document registered; upload each part to its presigned URL."),
        **problem_responses(
            {
                **_IDEMPOTENCY_PROBLEMS,
                404: "No such shipment.",
                409: "Shipment no longer accepts documents, or the `Idempotency-Key` is in use.",
            }
        ),
    },
)
async def register_document(
    shipment_id: UUID,
    body: DocumentCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
    ports: PortsDep,
) -> Response:
    """Register a document and get presigned URLs for a multipart upload straight to storage."""

    async def handle() -> StoredResponse:
        upload = await ports.shipments.register_document(shipment_id, body)
        location = f"/v1/shipments/{shipment_id}"
        return StoredResponse.json(201, upload, {"Location": location})

    return await run_idempotent(ports.idempotency, idempotency_key, request, handle)


@router.post(
    "/shipments/{shipment_id}/documents/{document_id}:finalize",
    tags=["documents"],
    status_code=202,
    response_model=FinalizeAccepted,
    responses={
        202: {
            "description": "Upload completed and processing queued.",
            "headers": {
                "Location": {
                    "description": "Poll this shipment for per-document and per-page status.",
                    "schema": {"type": "string"},
                },
                **_REPLAYED,
            },
        },
        **problem_responses(
            {
                **_IDEMPOTENCY_PROBLEMS,
                404: "No such shipment or document.",
                409: (
                    "Document already finalized or its parts don't match the upload, or the "
                    "`Idempotency-Key` is in use."
                ),
                429: "Too many pages pending (`overloaded`). Nothing was queued.",
            }
        ),
    },
)
async def finalize_document(
    shipment_id: UUID,
    document_id: UUID,
    body: DocumentFinalize,
    request: Request,
    idempotency_key: IdempotencyKey,
    ports: PortsDep,
) -> Response:
    """Complete the upload and queue the document for processing."""

    async def handle() -> StoredResponse:
        # Checked inside the idempotent block: a replay of an accepted finalize is never refused,
        # and a refusal releases the key so the client retries with the same one.
        retry_after = await ports.admission.retry_after()
        if retry_after is not None:
            raise OverloadedError(retry_after)
        accepted = await ports.shipments.finalize_document(shipment_id, document_id, body)
        return StoredResponse.json(202, accepted, {"Location": f"/v1/shipments/{shipment_id}"})

    return await run_idempotent(ports.idempotency, idempotency_key, request, handle)
