"""The generated REST SDK (clients/python) talks to the app: the contract holds end to end."""

from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
import pytest
from fastapi import FastAPI

from manifest_client import Client
from manifest_client.api.documents import finalize_document, register_document
from manifest_client.api.shipments import create_shipment, get_shipment, list_shipments
from manifest_client.models import (
    CompletedPart,
    DocumentCreate,
    DocumentFinalize,
    DocumentStatus,
    FinalizeAccepted,
    Problem,
    ShipmentCreate,
    UploadContentType,
)


@pytest.fixture
async def sdk(app: FastAPI) -> AsyncIterator[Client]:
    client = Client(base_url="http://test", raise_on_unexpected_status=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        client.set_async_httpx_client(http)
        yield client


async def test_upload_flow(sdk: Client) -> None:
    shipment = await create_shipment.asyncio(
        client=sdk, body=ShipmentCreate(reference="PO-77"), idempotency_key="s1"
    )
    assert not isinstance(shipment, Problem | None)

    upload = await register_document.asyncio(
        shipment.id,
        client=sdk,
        body=DocumentCreate(
            filename="invoice.pdf",
            content_type=UploadContentType.APPLICATIONPDF,
            size_bytes=1024,
        ),
        idempotency_key="d1",
    )
    assert not isinstance(upload, Problem | None)
    assert [part.part_number for part in upload.upload.parts] == [1]

    accepted = await finalize_document.asyncio(
        shipment.id,
        upload.document.id,
        client=sdk,
        body=DocumentFinalize(parts=[CompletedPart(part_number=1, etag="etag-1")]),
        idempotency_key="f1",
    )
    assert isinstance(accepted, FinalizeAccepted)

    detail = await get_shipment.asyncio(shipment.id, client=sdk)
    assert not isinstance(detail, Problem | None)
    assert [d.status for d in detail.documents] == [DocumentStatus.QUEUED]


async def test_overload_surfaces_as_typed_problem_with_retry_after(sdk: Client, fakes) -> None:
    shipment = await create_shipment.asyncio(
        client=sdk, body=ShipmentCreate(), idempotency_key="s1"
    )
    assert not isinstance(shipment, Problem | None)
    upload = await register_document.asyncio(
        shipment.id,
        client=sdk,
        body=DocumentCreate(
            filename="bl.png", content_type=UploadContentType.IMAGEPNG, size_bytes=10
        ),
        idempotency_key="d1",
    )
    assert not isinstance(upload, Problem | None)
    fakes.admission.retry_after_seconds = 12

    response = await finalize_document.asyncio_detailed(
        shipment.id,
        upload.document.id,
        client=sdk,
        body=DocumentFinalize(parts=[CompletedPart(part_number=1, etag="e")]),
        idempotency_key="f1",
    )
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert response.headers["retry-after"] == "12"
    assert isinstance(response.parsed, Problem)
    assert response.parsed.type_.endswith("/problems/overloaded")


async def test_pages_follow_next_cursor(sdk: Client) -> None:
    for n in range(3):
        await create_shipment.asyncio(
            client=sdk, body=ShipmentCreate(reference=f"PO-{n}"), idempotency_key=f"s{n}"
        )
    first = await list_shipments.asyncio(client=sdk, limit=2)
    assert not isinstance(first, Problem | None)
    assert isinstance(first.next_cursor, str)
    second = await list_shipments.asyncio(client=sdk, limit=2, cursor=first.next_cursor)
    assert not isinstance(second, Problem | None)
    assert [s.reference for s in [*first.items, *second.items]] == ["PO-2", "PO-1", "PO-0"]
    assert second.next_cursor is None
