"""The committed contracts match the app, and keep the promises the API makes."""

import json
from pathlib import Path
from typing import Any

import pytest

from manifest_api.contracts import graphql_sdl, openapi_json

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(openapi_json())
    return document


def operations(openapi: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, item in openapi["paths"].items()
        for method, operation in item.items()
    ]


def test_committed_contracts_match_the_app() -> None:
    # Fast local signal; `make contracts-check` also covers the generated clients.
    message = "contracts/ is stale: run `make contracts`"
    assert (CONTRACTS / "openapi.json").read_text() == openapi_json(), message
    assert (CONTRACTS / "schema.graphql").read_text() == graphql_sdl(), message


def test_openapi_is_3_1(openapi: dict[str, Any]) -> None:
    assert openapi["openapi"].startswith("3.1.")


def test_every_error_response_is_problem_json(openapi: dict[str, Any]) -> None:
    for method, path, operation in operations(openapi):
        for status, response in operation["responses"].items():
            media_types = set(response.get("content", {}))
            if status.startswith(("4", "5")):
                assert media_types == {"application/problem+json"}, (method, path, status)
                schema = response["content"]["application/problem+json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/Problem"}
            else:
                assert media_types <= {"application/json"}, (method, path, status)


def test_every_post_requires_an_idempotency_key(openapi: dict[str, Any]) -> None:
    posts = [(path, op) for method, path, op in operations(openapi) if method == "POST"]
    assert len(posts) == 3
    for path, operation in posts:
        headers = {p["name"]: p for p in operation["parameters"] if p["in"] == "header"}
        assert headers["Idempotency-Key"]["required"] is True, path
        assert "400" in operation["responses"], path


def test_finalize_documents_202_and_429_with_retry_after(openapi: dict[str, Any]) -> None:
    finalize = openapi["paths"]["/v1/shipments/{shipment_id}/documents/{document_id}:finalize"]
    responses = finalize["post"]["responses"]
    assert set(responses) >= {"202", "429"}
    assert "Retry-After" in responses["429"]["headers"]
    assert "job_id" in openapi["components"]["schemas"]["FinalizeAccepted"]["required"]


def test_list_shipments_is_keyset_paginated(openapi: dict[str, Any]) -> None:
    parameters = {p["name"] for p in openapi["paths"]["/v1/shipments"]["get"]["parameters"]}
    assert parameters == {"limit", "cursor"}
    assert "next_cursor" in openapi["components"]["schemas"]["ShipmentPage"]["properties"]


def test_operation_ids_are_stable_names(openapi: dict[str, Any]) -> None:
    assert sorted(op["operationId"] for _, _, op in operations(openapi)) == [
        "create_shipment",
        "finalize_document",
        "get_shipment",
        "list_shipments",
        "register_document",
    ]
