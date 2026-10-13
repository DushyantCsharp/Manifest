"""``python -m manifest_worker.monitor``: system-state gauges, sampled by one process.

No task sees the whole system, so one long-running process samples it at every metric export
(``OTEL_METRIC_EXPORT_INTERVAL``, 15 s) and sends it over OTLP with everything else:

    queue_depth{queue}            messages waiting in each stage queue (RabbitMQ management API)
    queue_unacked{queue}          delivered to a worker, not yet acknowledged
    dlq_depth{queue}              messages parked in each stage's dead-letter queue
    auto_approve_ratio{window}    auto-approved / routed, over shipments routed in the window
    review_queue_age_seconds      age of the oldest open review task (0 when none is open)
    review_queue_open             review tasks not yet approved

Run one per deployment. Every instance would report the same broker-wide numbers, so the
dashboards and alerts take the max by queue, never the sum (after a restart the old instance's
series lingers for a few minutes beside the new one's). A sample that fails (broker or
database unreachable) is skipped, so the series goes stale instead of reporting zeroes.
"""

import base64
import json
import logging
import signal
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import manifest_telemetry
from manifest_db.models import ReviewTask, Shipment
from manifest_db.session import sync_session_factory
from manifest_domain.lifecycle import ShipmentStatus
from manifest_domain.review import ReviewTaskStatus
from manifest_telemetry.metrics import gauge
from manifest_worker.app import settings
from manifest_worker.topology import STAGES, dlq_name

logger = logging.getLogger(__name__)

ROUTED = (ShipmentStatus.AUTO_APPROVED, ShipmentStatus.NEEDS_REVIEW, ShipmentStatus.APPROVED)
Samples = list[tuple[float, dict[str, str]]]


@dataclass(frozen=True)
class QueueCounts:
    ready: int
    unacked: int


def management_api(broker_url: str, api_url: str) -> tuple[str, dict[str, str]]:
    """The management API URL listing this broker's vhost, and its Basic auth header."""
    parsed = urllib.parse.urlsplit(broker_url)
    vhost = urllib.parse.unquote(parsed.path[1:]) or "/"
    user = urllib.parse.unquote(parsed.username or "guest")
    password = urllib.parse.unquote(parsed.password or "guest")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    columns = "name,messages_ready,messages_unacknowledged"
    url = f"{api_url.rstrip('/')}/queues/{urllib.parse.quote(vhost, safe='')}?columns={columns}"
    return url, {"Authorization": f"Basic {token}"}


def fetch_queues(url: str, headers: dict[str, str], timeout: float = 5.0) -> dict[str, QueueCounts]:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - our configured URL
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        rows: list[dict[str, Any]] = json.load(response)
    return {
        row["name"]: QueueCounts(
            int(row.get("messages_ready") or 0), int(row.get("messages_unacknowledged") or 0)
        )
        for row in rows
    }


class Cached[T]:
    """One fetch serves every gauge read in the same export (they're read back to back)."""

    def __init__(self, fetch: Callable[[], T], ttl: float = 2.0) -> None:
        self._fetch, self._ttl = fetch, ttl
        self._value: T | None = None
        self._at = float("-inf")
        self._lock = threading.Lock()

    def get(self) -> T:
        with self._lock:
            if self._value is None or time.monotonic() - self._at > self._ttl:
                self._value = self._fetch()
                self._at = time.monotonic()
            return self._value


def queue_samples(queues: dict[str, QueueCounts]) -> tuple[Samples, Samples, Samples]:
    """(depth, unacked, dlq depth) for every stage; a queue not declared yet counts as empty."""
    empty = QueueCounts(0, 0)
    depth = [(float(queues.get(s, empty).ready), {"queue": s}) for s in STAGES]
    unacked = [(float(queues.get(s, empty).unacked), {"queue": s}) for s in STAGES]
    dlq = []
    for s in STAGES:
        parked = queues.get(dlq_name(s), empty)
        dlq.append((float(parked.ready + parked.unacked), {"queue": dlq_name(s)}))
    return depth, unacked, dlq


@dataclass(frozen=True)
class ReviewState:
    open: int
    oldest_age_seconds: float
    auto_approved: int
    routed: int


def review_state(session: Session, window: timedelta, now: datetime | None = None) -> ReviewState:
    now = now or datetime.now(UTC)
    count, oldest = session.execute(
        select(func.count(), func.min(ReviewTask.created_at)).where(
            ReviewTask.status != ReviewTaskStatus.APPROVED
        )
    ).one()
    routed: dict[ShipmentStatus, int] = {}
    for status, n in session.execute(
        select(Shipment.status, func.count())
        .where(Shipment.status.in_(ROUTED), Shipment.updated_at >= now - window)
        .group_by(Shipment.status)
    ):
        routed[status] = n
    age = (now - oldest).total_seconds() if oldest is not None else 0.0
    return ReviewState(
        int(count),
        max(age, 0.0),
        int(routed.get(ShipmentStatus.AUTO_APPROVED, 0)),
        sum(int(n) for n in routed.values()),
    )


def ratio_samples(state: ReviewState, window: timedelta) -> Samples:
    """Nothing routed in the window: no sample (a gap), not a misleading 0 or 1."""
    if not state.routed:
        return []
    label = f"{int(window.total_seconds() // 60)}m"
    return [(state.auto_approved / state.routed, {"window": label})]


def guarded(sample: Callable[[], Samples], what: str) -> Callable[[], Samples]:
    """A failed read reports nothing this export (and is logged); the next one tries again."""

    def read() -> Samples:
        try:
            return sample()
        except Exception:
            logger.warning("monitor could not read %s", what, exc_info=True)
            return []

    return read


def register(
    queues: Callable[[], dict[str, QueueCounts]],
    review: Callable[[], ReviewState],
    window: timedelta,
) -> None:
    """The gauges, each read from a short-lived cache of one broker and one database query."""
    broker, database = Cached(queues), Cached(review)
    gauge(
        "queue_depth",
        guarded(lambda: queue_samples(broker.get())[0], "queues"),
        unit="{message}",
        description="Messages waiting in each stage queue.",
    )
    gauge(
        "queue_unacked",
        guarded(lambda: queue_samples(broker.get())[1], "queues"),
        unit="{message}",
        description="Messages delivered to a worker and not yet acknowledged, per stage queue.",
    )
    gauge(
        "dlq_depth",
        guarded(lambda: queue_samples(broker.get())[2], "queues"),
        unit="{message}",
        description="Messages parked in each stage's dead-letter queue.",
    )
    gauge(
        "auto_approve_ratio",
        guarded(lambda: ratio_samples(database.get(), window), "routing"),
        description="Share of shipments routed in the window that were auto-approved.",
    )
    gauge(
        "review_queue_age",
        guarded(lambda: [(database.get().oldest_age_seconds, {})], "review queue"),
        unit="s",
        description="Age of the oldest review task not yet approved (0 when none is open).",
    )
    gauge(
        "review_queue_open",
        guarded(lambda: [(float(database.get().open), {})], "review queue"),
        unit="{task}",
        description="Review tasks not yet approved.",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not manifest_telemetry.configure("manifest-monitor"):
        logger.error("telemetry is disabled (OTEL_SDK_DISABLED); nothing to export")
        return 1
    url, headers = management_api(settings.broker_url, settings.rabbitmq_api_url)
    sessions: sessionmaker[Session] = sync_session_factory()
    window = timedelta(minutes=settings.auto_approve_window_minutes)

    def review() -> ReviewState:
        with sessions() as session:
            return review_state(session, window)

    register(lambda: fetch_queues(url, headers), review, window)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logger.info("monitor sampling %s and the database", url.split("?")[0])
    stop.wait()
    manifest_telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
