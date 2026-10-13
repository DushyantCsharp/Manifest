"""Clients submitting shipment packs through the public API, then waiting for the verdict.

Each user, in a loop: create a shipment; register its three documents (the API takes documents
only while the shipment is open); upload each through its presigned URL(s) and finalize it;
then poll the shipment until it is routed (``needs_review`` or ``auto_approved``). Locust
records every API call; a pack's first-finalize-to-routed time is recorded as request type
``PIPELINE``. Users step up (``Steps``): each waits for its pack before sending the next, so the
step where throughput stops growing is the system's capacity.

Packs are the eval corpus' text-layer packs (``evals/golden/clean-*``, ``mism-*``), cycled, so
the worker's replayed model answers match their pages.

    LOADTEST_PACKS            where the packs are (default: evals/golden)
    LOADTEST_USERS            users per step, comma-separated (default: 4,8,16)
    LOADTEST_STEP_SECONDS     length of each step (default: 180)
    LOADTEST_RESULTS          append one JSON line per pack here (default: none)
    LOADTEST_TIMEOUT_SECONDS  give up on a pack after this long (default: 600)
"""

import itertools
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from locust import HttpUser, LoadTestShape, between, events, task

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(os.environ.get("LOADTEST_PACKS", ROOT / "evals" / "golden"))
DOCUMENTS = ("commercial_invoice", "packing_list", "bill_of_lading")
PACKS = sorted(p for p in GOLDEN.iterdir() if p.name.startswith(("clean-", "mism-")))
ROUTED = {"needs_review", "auto_approved", "approved"}
RESULTS = Path(os.environ["LOADTEST_RESULTS"]) if os.environ.get("LOADTEST_RESULTS") else None
TIMEOUT = float(os.environ.get("LOADTEST_TIMEOUT_SECONDS", "600"))
POLL_SECONDS = 1.0

_packs = itertools.cycle(PACKS)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


class Client(HttpUser):
    wait_time = between(1, 3)

    @task
    def submit_pack(self) -> None:
        pack = next(_packs)
        created = self.client.post(
            "/v1/shipments",
            json={"reference": pack.name},
            headers=_key(),
            name="POST /v1/shipments",
        )
        if created.status_code != 201:
            return
        shipment = created.json()["id"]
        registered = []
        for document in DOCUMENTS:
            data = (pack / f"{document}.pdf").read_bytes()
            response = self.client.post(
                f"/v1/shipments/{shipment}/documents",
                json={
                    "filename": f"{document}.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": len(data),
                },
                headers=_key(),
                name="POST /v1/shipments/{id}/documents",
            )
            if response.status_code != 201:
                return
            registered.append((data, response.json()))

        started = time.monotonic()
        for data, upload in registered:
            parts = []
            for part in upload["upload"]["parts"]:
                start = part["offset_bytes"]
                put = self.client.put(
                    part["url"], data=data[start : start + part["size_bytes"]], name="PUT part (S3)"
                )
                if put.status_code != 200:
                    return
                parts.append({"part_number": part["part_number"], "etag": put.headers["ETag"]})
            if not self._finalize(shipment, upload["document"]["id"], parts):
                return
        status = self._routed(shipment, started)
        elapsed = time.monotonic() - started
        events.request.fire(
            request_type="PIPELINE",
            name="pack: first finalize -> routed",
            response_time=elapsed * 1000,
            response_length=0,
            exception=None if status in ROUTED else TimeoutError(f"{shipment} still {status}"),
            context={},
        )
        if RESULTS:
            record = {
                "at": time.time(),
                "pack": pack.name,
                "shipment": shipment,
                "status": status,
                "seconds": round(elapsed, 3),
                "users": self.environment.runner.user_count if self.environment.runner else None,
            }
            with RESULTS.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

    def _finalize(self, shipment: str, document: str, parts: list[dict[str, Any]]) -> bool:
        """Finalize, honouring 429 + Retry-After (backpressure) with the same key."""
        key = _key()
        path = f"/v1/shipments/{shipment}/documents/{document}:finalize"
        while True:
            with self.client.post(
                path,
                json={"parts": parts},
                headers=key,
                name="POST /v1/shipments/{id}/documents/{id}:finalize",
                catch_response=True,
            ) as response:
                if response.status_code == 429:
                    response.success()  # backpressure working as designed, not an error
                    time.sleep(float(response.headers.get("Retry-After", "5")))
                    continue
                return bool(response.status_code == 202)

    def _routed(self, shipment: str, started: float) -> str:
        status = "unknown"
        while time.monotonic() - started < TIMEOUT:
            response = self.client.get(f"/v1/shipments/{shipment}", name="GET /v1/shipments/{id}")
            if response.status_code == 200:
                status = response.json()["status"]
                if status in ROUTED:
                    return status
            time.sleep(POLL_SECONDS)
        return status


class Steps(LoadTestShape):
    """Hold each user count for a step, then stop."""

    users: ClassVar[list[int]] = [
        int(n) for n in os.environ.get("LOADTEST_USERS", "4,8,16").split(",")
    ]
    step = float(os.environ.get("LOADTEST_STEP_SECONDS", "180"))

    def tick(self) -> tuple[int, float] | None:
        index = int(self.get_run_time() // self.step)
        if index >= len(self.users):
            return None
        return self.users[index], float(self.users[index])
