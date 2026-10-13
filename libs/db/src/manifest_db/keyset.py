"""Keyset pagination for the listings the API serves.

Each listing's order ends in the primary key, so it is a total order and a page boundary is a
row position, not a row count. A page therefore never repeats a row, nor skips one that
existed when paging began, however many rows are inserted or leave the listing meanwhile.
OFFSET counts rows instead, so every insert ahead of the offset shifts the rest of the listing
by one. Each query is a range scan on its listing's index.

The functions return statements, so the async API and the sync worker share them.
"""

from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select, tuple_

from manifest_db.models import ReviewTask, Shipment
from manifest_domain.review import ReviewPriority, ReviewTaskStatus

ShipmentKey = tuple[datetime, UUID]
ReviewTaskKey = tuple[ReviewPriority, datetime, UUID]


def shipments_page(limit: int, after: ShipmentKey | None = None) -> Select[Shipment]:
    """Newest first, ``ORDER BY created_at DESC, id DESC`` (index ``ix_shipments_keyset``)."""
    query = select(Shipment).order_by(Shipment.created_at.desc(), Shipment.id.desc())
    if after is not None:
        query = query.where(tuple_(Shipment.created_at, Shipment.id) < tuple_(*after))
    return query.limit(limit)


def shipment_key(shipment: Shipment) -> ShipmentKey:
    return shipment.created_at, shipment.id


def review_queue_page(
    limit: int,
    after: ReviewTaskKey | None = None,
    *,
    statuses: Collection[ReviewTaskStatus] | None = None,
    priorities: Collection[ReviewPriority] | None = None,
) -> Select[ReviewTask]:
    """Unfinished tasks, most urgent then oldest first: ``ORDER BY priority, created_at, id``
    (partial index ``ix_review_tasks_queue``). Approved tasks have left the queue.
    """
    query = (
        select(ReviewTask)
        .where(ReviewTask.status != ReviewTaskStatus.APPROVED)
        .order_by(ReviewTask.priority, ReviewTask.created_at, ReviewTask.id)
    )
    if statuses:
        query = query.where(ReviewTask.status.in_(statuses))
    if priorities:
        query = query.where(ReviewTask.priority.in_(priorities))
    if after is not None:
        position = tuple_(ReviewTask.priority, ReviewTask.created_at, ReviewTask.id)
        query = query.where(position > tuple_(*after))
    return query.limit(limit)


def review_task_key(task: ReviewTask) -> ReviewTaskKey:
    return task.priority, task.created_at, task.id
