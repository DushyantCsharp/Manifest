"""An app without adapters: health, GraphQL, problem responses, and 501 for every port."""

from collections.abc import AsyncIterator

import httpx
import pytest

from manifest_api.main import create_app


@pytest.fixture
async def unwired() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_healthz(unwired: httpx.AsyncClient) -> None:
    response = await unwired.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_graphql_is_mounted(unwired: httpx.AsyncClient) -> None:
    response = await unwired.post("/graphql", json={"query": "{ health }"})
    assert response.status_code == 200
    assert response.json() == {"data": {"health": "ok"}}


async def test_unwired_port_answers_501_problem(unwired: httpx.AsyncClient) -> None:
    response = await unwired.post("/v1/shipments", json={}, headers={"Idempotency-Key": "k1"})
    assert response.status_code == 501
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["type"].endswith("/problems/not-implemented")
    assert "has no adapter yet" in problem["detail"]


async def test_unknown_route_is_problem_json(unwired: httpx.AsyncClient) -> None:
    response = await unwired.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "instance": "/v1/nope",
    }
