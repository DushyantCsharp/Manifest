"""REST behaviour the contract promises: idempotency, backpressure, keyset pages, problems."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from manifest_api.pagination import ReviewTaskCursor
from manifest_api.rest.schemas import DocumentPage
from manifest_domain.lifecycle import DocumentStatus, PageStatus, TextSource

PROBLEM = "application/problem+json"
MIB = 1024 * 1024
ONE_PART = {"parts": [{"part_number": 1, "etag": "e1"}]}


def key(name: str) -> dict[str, str]:
    return {"Idempotency-Key": name}


async def create_shipment(client: httpx.AsyncClient, reference: str = "PO-1") -> dict[str, Any]:
    response = await client.post(
        "/v1/shipments", json={"reference": reference}, headers=key(f"create-{reference}")
    )
    assert response.status_code == 201, response.text
    return response.json()


async def register_pdf(client: httpx.AsyncClient, shipment_id: str, size: int) -> dict[str, Any]:
    response = await client.post(
        f"/v1/shipments/{shipment_id}/documents",
        json={"filename": "invoice.pdf", "content_type": "application/pdf", "size_bytes": size},
        headers=key(f"register-{shipment_id}"),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def finalize_url(client: httpx.AsyncClient) -> str:
    shipment = await create_shipment(client)
    document = (await register_pdf(client, shipment["id"], MIB))["document"]
    return f"/v1/shipments/{shipment['id']}/documents/{document['id']}:finalize"


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM
    problem: dict[str, Any] = response.json()
    assert problem["status"] == status
    assert problem["type"] == f"https://manifest.example/problems/{slug}"
    assert problem["instance"] == response.request.url.path
    return problem


# ---------------------------------------------------------------- idempotency


async def test_create_returns_201_with_location(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/shipments", json={"reference": "PO-9"}, headers=key("a"))
    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"/v1/shipments/{body['id']}"
    assert body["status"] == "open"
    assert "idempotent-replayed" not in response.headers


async def test_retry_with_same_key_replays_without_repeating(client: httpx.AsyncClient, fakes):
    first = await client.post("/v1/shipments", json={"reference": "PO-1"}, headers=key("k"))
    retry = await client.post("/v1/shipments", json={"reference": "PO-1"}, headers=key("k"))

    assert retry.status_code == 201
    assert retry.content == first.content
    assert retry.headers["location"] == first.headers["location"]
    assert retry.headers["idempotent-replayed"] == "true"
    assert len(fakes.shipments.shipments) == 1


async def test_same_key_different_body_is_rejected(client: httpx.AsyncClient, fakes) -> None:
    await client.post("/v1/shipments", json={"reference": "PO-1"}, headers=key("k"))
    response = await client.post("/v1/shipments", json={"reference": "PO-2"}, headers=key("k"))
    assert_problem(response, 422, "idempotency-key-reused")
    assert len(fakes.shipments.shipments) == 1


async def test_missing_key_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/shipments", json={"reference": "PO-1"})
    problem = assert_problem(response, 400, "idempotency-key-missing")
    assert problem["errors"] == [{"detail": "Field required", "parameter": "Idempotency-Key"}]


async def test_key_in_flight_is_409(client: httpx.AsyncClient, fakes) -> None:
    fakes.shipments.hold = asyncio.Event()
    request = {"json": {"reference": "PO-1"}, "headers": key("slow")}
    first = asyncio.create_task(client.post("/v1/shipments", **request))
    await fakes.shipments.entered.wait()

    concurrent = await client.post("/v1/shipments", **request)
    assert_problem(concurrent, 409, "idempotency-key-in-flight")

    fakes.shipments.hold.set()
    assert (await first).status_code == 201


# ---------------------------------------------------------------- uploads and backpressure


async def test_register_document_returns_parts_covering_the_file(client: httpx.AsyncClient):
    shipment = await create_shipment(client)
    size = 20 * MIB + 5
    upload = await register_pdf(client, shipment["id"], size)

    assert upload["document"]["status"] == "awaiting_upload"
    parts = upload["upload"]["parts"]
    assert [p["part_number"] for p in parts] == [1, 2, 3]
    assert [p["offset_bytes"] for p in parts] == [0, 8 * MIB, 16 * MIB]
    assert sum(p["size_bytes"] for p in parts) == size


async def test_finalize_is_202_with_job_and_location(client: httpx.AsyncClient) -> None:
    url = await finalize_url(client)
    response = await client.post(url, json=ONE_PART, headers=key("f"))

    assert response.status_code == 202
    assert response.headers["location"] == url.split("/documents/")[0]
    body = response.json()
    assert UUID(body["job_id"])
    assert body["status"] == "queued"


async def test_overloaded_finalize_is_429_and_retry_with_same_key_succeeds(
    client: httpx.AsyncClient, fakes
) -> None:
    url = await finalize_url(client)

    fakes.admission.retry_after_seconds = 30
    refused = await client.post(url, json=ONE_PART, headers=key("f"))
    assert_problem(refused, 429, "overloaded")
    assert refused.headers["retry-after"] == "30"
    assert fakes.shipments.finalized == 0

    fakes.admission.retry_after_seconds = None
    accepted = await client.post(url, json=ONE_PART, headers=key("f"))
    assert accepted.status_code == 202
    assert "idempotent-replayed" not in accepted.headers
    assert fakes.shipments.finalized == 1


async def test_replayed_finalize_is_not_refused_when_overloaded(client: httpx.AsyncClient, fakes):
    url = await finalize_url(client)
    await client.post(url, json=ONE_PART, headers=key("f"))

    fakes.admission.retry_after_seconds = 30
    replay = await client.post(url, json=ONE_PART, headers=key("f"))
    assert replay.status_code == 202
    assert replay.headers["idempotent-replayed"] == "true"


async def test_finalize_twice_with_new_key_conflicts(client: httpx.AsyncClient) -> None:
    url = await finalize_url(client)
    await client.post(url, json=ONE_PART, headers=key("f1"))
    assert_problem(await client.post(url, json=ONE_PART, headers=key("f2")), 409, "conflict")


async def test_validation_errors_point_into_the_body(client: httpx.AsyncClient) -> None:
    shipment = await create_shipment(client)
    response = await client.post(
        f"/v1/shipments/{shipment['id']}/documents",
        json={"filename": "x.pdf", "content_type": "application/pdf", "size_bytes": 0},
        headers=key("bad"),
    )
    problem = assert_problem(response, 422, "validation-error")
    assert [e["pointer"] for e in problem["errors"]] == ["#/size_bytes"]


async def test_duplicate_part_numbers_are_rejected(client: httpx.AsyncClient) -> None:
    url = await finalize_url(client)
    parts = [{"part_number": 1, "etag": "a"}, {"part_number": 1, "etag": "b"}]
    response = await client.post(url, json={"parts": parts}, headers=key("dup"))
    problem = assert_problem(response, 422, "validation-error")
    assert problem["errors"][0]["pointer"] == "#/parts"


# ---------------------------------------------------------------- reads


async def test_shipment_detail_has_document_and_page_status(client: httpx.AsyncClient, fakes):
    shipment = await create_shipment(client)
    await register_pdf(client, shipment["id"], MIB)
    document = fakes.shipments.shipments[UUID(shipment["id"])].documents[0]
    document.status = DocumentStatus.PARTIALLY_EXTRACTED
    document.page_count = 2
    document.extraction_schema_version = 1
    document.pages = [
        DocumentPage(
            page_number=1,
            status=PageStatus.EXTRACTED,
            text_source=TextSource.NATIVE,
            attempts=1,
            error=None,
        ),
        DocumentPage(
            page_number=2, status=PageStatus.FAILED, text_source=None, attempts=3, error="timeout"
        ),
    ]

    response = await client.get(f"/v1/shipments/{shipment['id']}")
    assert response.status_code == 200
    [doc] = response.json()["documents"]
    assert doc["status"] == "partially_extracted"
    assert doc["extraction_schema_version"] == 1
    assert [(p["page_number"], p["status"], p["error"]) for p in doc["pages"]] == [
        (1, "extracted", None),
        (2, "failed", "timeout"),
    ]


async def test_unknown_shipment_is_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/shipments/00000000-0000-0000-0000-000000000000")
    assert_problem(response, 404, "not-found")


async def test_keyset_pages_visit_every_shipment_once_newest_first(
    client: httpx.AsyncClient, fakes
) -> None:
    for n in range(7):
        await create_shipment(client, f"PO-{n}")
    # Ties on created_at must be broken by id, or rows go missing between pages.
    tied = datetime(2026, 10, 1, 12, tzinfo=UTC)
    for shipment in list(fakes.shipments.shipments.values())[2:5]:
        shipment.created_at = tied
    expected = sorted(
        fakes.shipments.shipments.values(), key=lambda s: (s.created_at, s.id), reverse=True
    )

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/v1/shipments", params=params)).json()
        seen += [item["id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(s.id) for s in expected]
    assert pages == 3


async def test_bad_cursors_are_400(client: httpx.AsyncClient) -> None:
    foreign = ReviewTaskCursor(0, datetime(2026, 1, 1, tzinfo=UTC), UUID(int=1)).encode()
    for cursor in ("not-a-cursor", foreign):
        response = await client.get("/v1/shipments", params={"cursor": cursor})
        assert_problem(response, 400, "invalid-cursor")


async def test_limit_is_bounded(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/shipments", params={"limit": 101})
    problem = assert_problem(response, 422, "validation-error")
    assert problem["errors"][0]["parameter"] == "limit"


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("OPTIONS", "/v1/shipments", "GET, POST"),
        ("DELETE", "/v1/shipments", "GET, POST"),
        ("PUT", "/v1/shipments/7f1c6f6e-5d2a-4c1e-9a49-1b3f0f4f9a10", "GET"),
    ],
)
async def test_method_not_allowed_names_every_method_the_path_serves(
    client: httpx.AsyncClient, method: str, path: str, allow: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 405
    assert response.headers["allow"] == allow  # RFC 9110: every method, not one route's
    assert response.headers["content-type"].startswith("application/problem+json")
