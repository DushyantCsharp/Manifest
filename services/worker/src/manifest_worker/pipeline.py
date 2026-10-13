"""Celery mechanics every stage shares: retries, rate-limit re-queues, parking in the DLQ.

- Transient errors (``errors.TRANSIENT``) retry via ``autoretry_for`` with exponential backoff
  and full jitter, at most ``max_retries`` times.
- A rate-limited call is re-queued with a countdown (``defer``) without spending a retry.
- A message that fails for good is copied, standalone, into ``<stage>.dlq`` (``park``) with
  the reason in ``x-manifest-*`` headers; ``python -m manifest_worker.dlq`` redrives it.
"""

import math
import random
from datetime import UTC, datetime
from typing import Any, NoReturn

from celery import Task
from celery.exceptions import Retry

from manifest_worker.app import settings
from manifest_worker.errors import TRANSIENT, TransientError
from manifest_worker.topology import dead_letter_queue

POISON_AFTER = 3  # deliveries that ended with a dead worker before we stop trying (limit: 5)
MAX_DEFERRALS = 30
DEFERRALS_HEADER = "x-manifest-deferrals"


class PipelineTask(Task):  # type: ignore[misc]
    stage = ""
    autoretry_for = TRANSIENT
    max_retries = settings.max_retries
    retry_backoff = settings.retry_backoff
    retry_backoff_max = settings.retry_backoff_max
    retry_jitter = True
    acks_late = True
    reject_on_worker_lost = True

    def on_failure(
        self, exc: BaseException, task_id: str, args: Any, kwargs: Any, einfo: Any
    ) -> None:
        park(self, exc, failure="failed")


def failed_deliveries(task: Task) -> int:
    """How often the broker already delivered this message without an ack (worker died)."""
    return int(task.request.get("x-delivery-count") or 0)


def deferrals(task: Task) -> int:
    return int(task.request.get(DEFERRALS_HEADER) or 0)


def defer(task: Task, seconds: float) -> NoReturn:
    """Re-queue the current message after ``seconds`` without spending a retry."""
    count = deferrals(task) + 1
    if count > MAX_DEFERRALS:
        raise TransientError(f"still rate limited after {MAX_DEFERRALS} deferrals")
    countdown = max(1, math.ceil(seconds + random.random()))  # noqa: S311 - jitter, not crypto
    signature = task.signature_from_request(
        task.request,
        countdown=countdown,
        retries=task.request.retries,
        headers={DEFERRALS_HEADER: count},
    )
    signature.apply_async()
    raise Retry(f"rate limited; re-queued for {countdown}s", when=countdown, sig=signature)


def park(task: Task, exc: BaseException, *, failure: str) -> None:
    """Copy the current message into its stage's DLQ as a standalone task (no chord)."""
    headers = {
        "x-manifest-stage": task.stage,
        "x-manifest-failure": failure,
        "x-manifest-exception": type(exc).__name__,
        "x-manifest-error": str(exc)[:1000],
        "x-manifest-retries": task.request.retries,
        "x-manifest-original-id": task.request.id,
        "x-manifest-parked-at": datetime.now(UTC).isoformat(),
    }
    task.apply_async(
        args=task.request.args,
        kwargs=task.request.kwargs,
        queue=dead_letter_queue(task.stage),
        headers=headers,
    )
