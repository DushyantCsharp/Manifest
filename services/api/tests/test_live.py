"""Against a real HTTP server: generated clients work, and it serves the committed contracts.

The app runs in uvicorn on a free local port for the duration of the module.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from manifest_api.main import create_app
from manifest_review_client import Client

pytestmark = pytest.mark.integration

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


@pytest.fixture(scope="module")
def api_url() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


async def test_generated_graphql_client_reaches_the_live_api(api_url: str) -> None:
    async with Client(url=f"{api_url}/graphql") as client:
        assert (await client.health()).health == "ok"


async def test_served_openapi_is_the_committed_contract(api_url: str) -> None:
    async with httpx.AsyncClient(base_url=api_url) as http:
        served = (await http.get("/openapi.json")).raise_for_status().json()
    assert served == json.loads((CONTRACTS / "openapi.json").read_text())
