"""The monitor's gauges: queue and DLQ depth from the broker, review state from the database."""

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from kombu import Connection
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import ReviewTask, Shipment
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.lifecycle import ShipmentStatus
from manifest_domain.review import ReviewPriority, ReviewReason, ReviewTaskStatus
from manifest_telemetry.testing import capture
from manifest_worker import monitor
from manifest_worker.monitor import QueueCounts, ReviewState
from manifest_worker.topology import declare, work_queue

WINDOW = timedelta(hours=1)


def test_the_management_url_follows_the_broker_url() -> None:
    url, headers = monitor.management_api(
        "amqp://manifest:s%40cret@rabbit:5672/pipeline", "http://rabbit:15672/api/"
    )
    assert url.startswith("http://rabbit:15672/api/queues/pipeline?columns=name,")
    assert headers["Authorization"] == "Basic bWFuaWZlc3Q6c0BjcmV0"  # manifest:s@cret
    default, _ = monitor.management_api("amqp://u:p@rabbit:5672//", "http://rabbit:15672/api")
    assert "/queues/%2F?" in default  # the default vhost "/"


def test_every_stage_reports_depth_even_before_its_queue_exists() -> None:
    depth, unacked, dlq = monitor.queue_samples(
        {"page_extract": QueueCounts(7, 2), "page_extract.dlq": QueueCounts(3, 1)}
    )
    assert {labels["queue"]: value for value, labels in depth} == {
        "classify": 0, "page_extract": 7, "reconcile": 0, "hs_classify": 0,
    }  # fmt: skip
    assert (2.0, {"queue": "page_extract"}) in unacked
    assert (4.0, {"queue": "page_extract.dlq"}) in dlq  # being inspected still counts


def test_no_routing_in_the_window_is_a_gap_not_a_zero() -> None:
    assert monitor.ratio_samples(ReviewState(0, 0.0, 0, 0), WINDOW) == []
    assert monitor.ratio_samples(ReviewState(0, 0.0, 3, 4), WINDOW) == [(0.75, {"window": "60m"})]


def test_a_failed_read_skips_the_export() -> None:
    def broken() -> list[tuple[float, dict[str, str]]]:
        raise ConnectionError("broker down")

    assert monitor.guarded(broken, "queues")() == []


def test_one_fetch_serves_reads_in_the_same_export() -> None:
    calls: list[int] = []

    def fetch() -> int:
        calls.append(1)
        return len(calls)

    cached = monitor.Cached(fetch, ttl=60)
    assert cached.get() == cached.get() == 1
    assert len(calls) == 1


def test_registered_gauges_report_the_samples() -> None:
    telemetry = capture()
    # Gauges are process-wide: a window no other test uses identifies this registration.
    minutes = 1000 + uuid.uuid4().int % 100_000
    queues = {"classify": QueueCounts(5, 1), "hs_classify.dlq": QueueCounts(2, 0)}
    state = ReviewState(open=4, oldest_age_seconds=18_000.0, auto_approved=1, routed=4)
    monitor.register(lambda: queues, lambda: state, timedelta(minutes=minutes))
    assert telemetry.total("queue_depth", {"queue": "classify"}) >= 5
    assert telemetry.total("dlq_depth", {"queue": "hs_classify.dlq"}) >= 2
    assert telemetry.total("auto_approve_ratio", {"window": f"{minutes}m"}) == 0.25
    assert telemetry.total("review_queue_age") >= 18_000


# ---------------------------------------------------------------- against real services


@pytest.mark.integration
def test_queue_depth_comes_from_the_management_api(vhost: str, broker: Any) -> None:
    with Connection(vhost) as connection:
        declare(connection)
        producer = connection.Producer()
        queue = work_queue("reconcile")
        for _ in range(3):
            producer.publish({"n": 1}, exchange=queue.exchange, routing_key="reconcile")
    url, headers = monitor.management_api(vhost, broker.management)
    found: dict[str, QueueCounts] = {}
    for _ in range(40):  # the management API's statistics lag the broker by a few seconds
        found = monitor.fetch_queues(url, headers)
        if found.get("reconcile") == QueueCounts(3, 0):
            break
        time.sleep(0.5)
    assert found["reconcile"] == QueueCounts(3, 0)
    assert found["reconcile.dlq"] == QueueCounts(0, 0)


@pytest.fixture
def sessions(postgres_url: str) -> Iterator[sessionmaker[Session]]:
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        yield sync_session_factory(database)


@pytest.mark.integration
def test_review_state_from_the_database(sessions: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    with sessions.begin() as session:
        shipments = [
            Shipment(status=status)
            for status in (
                ShipmentStatus.AUTO_APPROVED,
                ShipmentStatus.AUTO_APPROVED,
                ShipmentStatus.NEEDS_REVIEW,
                ShipmentStatus.PROCESSING,
            )
        ]
        session.add_all(shipments)
        session.flush()
        for shipment, age, status in (
            (shipments[2], timedelta(hours=5), ReviewTaskStatus.OPEN),
            (shipments[3], timedelta(hours=1), ReviewTaskStatus.IN_PROGRESS),
            (shipments[0], timedelta(days=2), ReviewTaskStatus.APPROVED),  # closed: ignored
        ):
            session.add(
                ReviewTask(
                    shipment_id=shipment.id,
                    priority=ReviewPriority.NORMAL,
                    status=status,
                    reasons=[ReviewReason.LOW_CONFIDENCE],
                    created_at=now - age,
                )
            )
    with sessions() as session:
        state = monitor.review_state(session, WINDOW, now)
    assert state.open == 2
    assert 5 * 3600 - 5 <= state.oldest_age_seconds <= 5 * 3600 + 60
    assert (state.auto_approved, state.routed) == (2, 3)  # processing isn't routed yet
    with sessions() as session:
        later = monitor.review_state(session, WINDOW, now + timedelta(hours=2))
    assert later.routed == 0  # routed before the window
