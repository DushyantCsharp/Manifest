"""Schemathesis fuzzes the REST API against the committed OpenAPI contract.

The app runs in uvicorn, wired to the in-memory fakes, and ``st run`` (from the separately
locked ``tools/codegen`` environment) generates requests from ``contracts/openapi.json``:
documented examples, boundary values, then random and invalid data, plus stateful sequences
along the spec's links. Every response must match the contract: documented status codes,
response schemas, content types and headers, RFC 9457 problems for rejected input, and no
5xx. ``schemathesis_hooks.py`` teaches it the two rules JSON Schema can't state. A JUnit
report is written to the pytest temp dir.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "contracts" / "openapi.json"
HOOKS = Path(__file__).with_name("schemathesis_hooks.py")  # rules JSON Schema can't state
EXAMPLES = os.environ.get("SCHEMATHESIS_MAX_EXAMPLES", "60")


@pytest.fixture
def api_url(app: FastAPI) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
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


def test_rest_api_honours_its_openapi_contract(api_url: str, tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is needed to run schemathesis from tools/codegen"
    environment = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    environment["SCHEMATHESIS_HOOKS"] = str(HOOKS)
    command = [
        uv, "run", "--project", str(ROOT / "tools" / "codegen"), "--locked", "--quiet",
        "st", "run", str(CONTRACT),
        "--url", api_url,
        "--checks", "all",
        "--max-examples", EXAMPLES,
        "--seed", "2022",
        "--workers", "1",
        "--request-timeout", "10",
        "--no-color",
        "--output-truncate", "true",
        "--report", "junit",
        "--report-dir", str(tmp_path),
    ]  # fmt: skip
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, capture_output=True, text=True, env=environment, timeout=600, check=False
    )
    assert result.returncode == 0, result.stdout[-20_000:] + result.stderr[-5_000:]
