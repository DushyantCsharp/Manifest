"""Writes several stages share: routing a shipment to human review."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from manifest_db.models import ReviewTask, Shipment
from manifest_domain.lifecycle import ShipmentStatus
from manifest_domain.review import ReviewPriority, ReviewReason, ReviewTaskStatus


def open_review(
    session: Session,
    shipment_id: UUID,
    reasons: Iterable[ReviewReason],
    priority: ReviewPriority = ReviewPriority.HIGH,
) -> None:
    """Add ``reasons`` to the shipment's open review task, creating it if needed."""
    task = session.scalar(
        select(ReviewTask)
        .where(ReviewTask.shipment_id == shipment_id)
        .where(ReviewTask.status != ReviewTaskStatus.APPROVED)
        .with_for_update()
    )
    if task is None:
        task = ReviewTask(shipment_id=shipment_id, priority=priority, reasons=[])
        session.add(task)
    task.reasons = sorted(set(task.reasons) | set(reasons))
    task.priority = min(task.priority, priority)
    shipment = session.get(Shipment, shipment_id)
    if shipment is not None:
        shipment.status = ShipmentStatus.NEEDS_REVIEW
