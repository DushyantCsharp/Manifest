"""Failure-handling demo: a burst of shipments shed by admission control, then a storage outage
the pipeline retries through. What README's demo GIF records (docs/demo.tape, `make demo`).

    uv run python scripts/demo.py --prepare   start the app containers for the demo
    uv run python scripts/demo.py             run the scenario, narrating it
    uv run python scripts/demo.py --stop      stop the app containers

The system is the real one (the api, worker and monitor images, Postgres, RabbitMQ, MinIO,
Redis) with the load test's replayed model answers, so nothing is called or billed. For the
demo the API sheds finalizes above 12 pending pages (429, Retry-After 5 s) and retries back
off from 4 s, so a short outage is retried through rather than parked. The packs are the
corpus' shortest invoices, which keeps the (sequential) HS stage, and the demo, short.

The outage starts while the first admitted documents are being split and extracted: workers'
storage calls fail and are retried with backoff; finalizes that need storage answer 500 and
the client retries them with the same Idempotency-Key, like the shed ones.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import loadtest
from loadtest import API, GOLDEN, ROUTED

PACKS = 12
HIGH_WATER = "12"
RETRY_AFTER = "5"
OUTAGE_AFTER_SECONDS = 2  # after the first admission round
OUTAGE_SECONDS = 8

RED, GREEN, YELLOW, BLUE, DIM, BOLD, RESET = (
    "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[2m", "\033[1m", "\033[0m",
)  # fmt: skip

started = time.monotonic()
_print_lock = threading.Lock()


def say(text: str, colour: str = "") -> None:
    elapsed = int(time.monotonic() - started)
    stamp = f"{DIM}[{elapsed // 60:02d}:{elapsed % 60:02d}]{RESET}"
    with _print_lock:
        print(f"{stamp} {colour}{text}{RESET}", flush=True)


def call(method: str, path: str, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
    request = urllib.request.Request(  # noqa: S310 - the local API
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Idempotency-Key": key or str(uuid.uuid4())},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.load(response)


def upload(pack: str) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """A shipment with its three documents registered and uploaded; finalize bodies to send."""
    folder = GOLDEN / pack
    shipment = call("POST", "/v1/shipments", {"reference": pack})["id"]
    pending = []
    for document in loadtest.DOCUMENTS:
        data = (folder / f"{document}.pdf").read_bytes()
        body = {"filename": f"{document}.pdf", "content_type": "application/pdf"}
        registered = call(
            "POST", f"/v1/shipments/{shipment}/documents", body | {"size_bytes": len(data)}
        )
        parts = []
        for part in registered["upload"]["parts"]:
            chunk = data[part["offset_bytes"] : part["offset_bytes"] + part["size_bytes"]]
            put = urllib.request.Request(part["url"], data=chunk, method="PUT")  # noqa: S310
            with urllib.request.urlopen(put, timeout=60) as response:  # noqa: S310
                parts.append({"part_number": part["part_number"], "etag": response.headers["ETag"]})
        path = f"/v1/shipments/{shipment}/documents/{registered['document']['id']}:finalize"
        pending.append((path, {"parts": parts}))
    return shipment, pending


def finalize(item: tuple[str, dict[str, Any], str]) -> tuple[int, int]:
    """(status, Retry-After seconds) for one finalize; the key is reused across retries."""
    path, body, key = item
    try:
        call("POST", path, body, key)
    except urllib.error.HTTPError as exc:
        return exc.code, int(exc.headers.get("Retry-After") or 0)
    return 202, 0


def worker_retries(since: str) -> list[str]:
    """The worker's retry log lines since ``since`` (Celery: "retry: Retry in Ns: Error(...)")."""
    logs = subprocess.run(
        [*loadtest.COMPOSE, "logs", "--no-color", "--since", since, "worker"],
        capture_output=True,
        text=True,
        check=False,
        env=loadtest.compose_env(),
    ).stdout
    return [line for line in logs.splitlines() if "retry: Retry in" in line]


def storage(action: str) -> None:
    subprocess.run(
        [*loadtest.COMPOSE, action, "minio"],
        capture_output=True,
        check=True,
        env=loadtest.compose_env(),
    )


def progress(shipments: list[str]) -> tuple[dict[str, str], int, int]:
    """Each shipment's status, and pages extracted out of pages known."""
    statuses, extracted, pages = {}, 0, 0
    for shipment in shipments:
        detail = call("GET", f"/v1/shipments/{shipment}")
        statuses[shipment] = str(detail["status"])
        for document in detail["documents"]:
            pages += max(len(document["pages"]), 1)
            extracted += sum(1 for page in document["pages"] if page["status"] == "extracted")
    return statuses, extracted, pages


def dead_letters() -> int:
    request = urllib.request.Request(  # noqa: S310 - the local broker
        loadtest.RABBITMQ_QUEUES, headers={"Authorization": loadtest.RABBITMQ_AUTH}
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        rows: list[dict[str, Any]] = json.load(response)
    return sum(int(r.get("messages") or 0) for r in rows if str(r.get("name")).endswith(".dlq"))


def admit(waiting: list[tuple[str, dict[str, Any], str]], on_first: Any) -> int:
    """Finalize everything at once, then retry what was shed (after Retry-After) or failed;
    how many were shed."""
    shed_total, first = 0, True
    while waiting:
        with ThreadPoolExecutor(max_workers=len(waiting)) as pool:
            outcomes = list(pool.map(finalize, waiting))
        accepted = sum(1 for status, _ in outcomes if status == 202)
        again = [item for item, (status, _) in zip(waiting, outcomes, strict=True) if status != 202]
        shed = sum(1 for status, _ in outcomes if status == 429)
        failed = sorted({status for status, _ in outcomes if status not in (202, 429)})
        retry_after = max((after for status, after in outcomes if status == 429), default=5)
        verb = "finalize" if first else "retry"
        parts = [f"{accepted} queued"]
        if shed:
            parts.append(f"{shed} shed (429, Retry-After {retry_after} s)")
        if failed:
            n = len(again) - shed
            parts.append(f"{n} failed ({'/'.join(map(str, failed))}: storage unreachable)")
        colour = GREEN if not again else (RED if failed else YELLOW)
        say(f"{verb}: {', '.join(parts)}" + ("" if again else "; every document is in"), colour)
        if first:
            on_first()
            first = False
        shed_total += shed
        if again:
            time.sleep(retry_after)
        waiting = again
    return shed_total


def outage(seen: set[str], stop: threading.Event) -> None:
    """Take storage down for a while, and narrate the retries it causes until ``stop``."""
    since = datetime.now(UTC).isoformat()
    say(f"storage outage: MinIO down for {OUTAGE_SECONDS} s, mid-extraction", RED)
    storage("stop")
    down_until = time.monotonic() + OUTAGE_SECONDS
    back = False
    while not stop.is_set():
        if not back and time.monotonic() >= down_until:
            storage("start")
            say("storage back", GREEN)
            back = True
        fresh = [line for line in worker_retries(since) if line not in seen]
        seen.update(fresh)
        if fresh:
            tasks = sorted({line.split("Task ", 1)[-1].split("[", 1)[0] for line in fresh})
            error = fresh[0].split("Retry in ", 1)[-1].split(":", 1)[-1].split("(", 1)[0].strip()
            names = ", ".join(t.removeprefix("manifest.") for t in tasks)
            retries = "1 task retry" if len(fresh) == 1 else f"{len(fresh)} task retries"
            say(f"{retries} scheduled ({names}): {error}, backing off", BLUE)
        stop.wait(2)
    if not back:
        storage("start")


def shortest_invoices(count: int) -> list[str]:
    def lines(pack: str) -> int:
        truth = json.loads((GOLDEN / pack / "commercial_invoice.json").read_text())
        return len(truth["line_items"])

    clean = sorted(p.name for p in GOLDEN.iterdir() if p.name.startswith("clean-"))
    return sorted(clean, key=lambda pack: (lines(pack), pack))[:count]


def scenario() -> int:
    packs = shortest_invoices(PACKS)
    print(f"{BOLD}Manifest: a burst and a storage outage, handled{RESET}")
    print(f"{DIM}Real API, worker, Postgres, RabbitMQ, MinIO; replayed model answers.{RESET}\n")
    say(f"{PACKS} shipments arrive at once: {PACKS * 3} documents, uploaded straight to storage")
    with ThreadPoolExecutor(max_workers=PACKS) as pool:
        uploaded = list(pool.map(upload, packs))
    shipments = [shipment for shipment, _ in uploaded]
    waiting = [(path, body, str(uuid.uuid4())) for _, pending in uploaded for path, body in pending]
    say(f"all {len(waiting)} finalized at once; the API admits up to {HIGH_WATER} pending pages")

    seen: set[str] = set()
    stop = threading.Event()
    watcher = threading.Thread(target=outage, args=(seen, stop), daemon=True)
    shed = admit(waiting, lambda: threading.Timer(OUTAGE_AFTER_SECONDS, watcher.start).start())

    deadline, last_tick = time.monotonic() + 600, 0.0
    statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        statuses, extracted, pages = progress(shipments)
        done = sum(1 for status in statuses.values() if status in ROUTED)
        if time.monotonic() - last_tick > 10 or done == len(shipments):
            say(f"{extracted}/{pages} pages extracted, {done}/{len(shipments)} shipments routed")
            last_tick = time.monotonic()
        if done == len(shipments):
            break
        time.sleep(1)
    stop.set()
    watcher.join(timeout=30)
    parked = dead_letters()
    auto = sum(1 for status in statuses.values() if status == "auto_approved")
    review = sum(1 for status in statuses.values() if status == "needs_review")
    ok = parked == 0 and auto + review == len(shipments)
    colour = GREEN if ok else RED
    say(
        f"done: {auto + review}/{len(shipments)} shipments routed "
        f"({auto} auto-approved, {review} to review)",
        colour,
    )
    say(
        f"{shed} finalizes shed and retried, {len(seen)} task retries, {parked} dead letters",
        colour,
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prepare", action="store_true", help="start the app for the demo")
    parser.add_argument("--stop", action="store_true", help="stop the app containers")
    args = parser.parse_args()
    if args.stop:
        loadtest.stop_app()
        return 0
    if args.prepare:
        demo = {
            "PENDING_PAGES_HIGH_WATER": HIGH_WATER,
            "ADMISSION_RETRY_AFTER_SECONDS": RETRY_AFTER,
            "RETRY_BACKOFF": "4",
        }
        loadtest.start_app("demo", 8, loadtest.cassettes(), extra=demo)
        loadtest.wait_healthy()
        return 0
    return scenario()


if __name__ == "__main__":
    sys.exit(main())
