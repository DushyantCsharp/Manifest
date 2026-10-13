"""Backfill for extraction schema v2: re-extract v1 documents whose line items lack an origin.

Registered by migration 0004; migration 0005 refuses to apply until it has finished. Selects
current (not superseded) v1 extractions with any line item missing ``country_of_origin``; the
runner (``reextract``) is resumable, single-runner and rate-limited.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine

from manifest_db.models import Extraction, LineItem
from manifest_worker.backfills import reextract
from manifest_worker.backfills.ratelimit import TokenBucket
from manifest_worker.backfills.reextract import BackfillLockedError, Enqueue, Outcome

NAME = "line_items.country_of_origin"
FROM_SCHEMA_VERSION = 1
TO_SCHEMA_VERSION = 2

__all__ = ["NAME", "SPEC", "BackfillLockedError", "Outcome", "candidates", "celery_enqueue", "run"]


def candidates(after: UUID | None, limit: int) -> Select[tuple[UUID]]:
    query = (
        select(Extraction.document_id)
        .join(LineItem, LineItem.extraction_id == Extraction.id)
        .where(
            Extraction.superseded_at.is_(None),
            Extraction.schema_version == FROM_SCHEMA_VERSION,
            LineItem.country_of_origin.is_(None),
        )
        .distinct()
        .order_by(Extraction.document_id)
        .limit(limit)
    )
    return query if after is None else query.where(Extraction.document_id > after)


SPEC = reextract.Spec(NAME, TO_SCHEMA_VERSION, candidates)
celery_enqueue = reextract.celery_enqueue(TO_SCHEMA_VERSION)


async def run(
    engine: AsyncEngine,
    enqueue: Enqueue,
    *,
    limiter: TokenBucket,
    batch_size: int = 100,
    max_batches: int | None = None,
    restart: bool = False,
) -> Outcome:
    return await reextract.run(
        SPEC,
        engine,
        enqueue,
        limiter=limiter,
        batch_size=batch_size,
        max_batches=max_batches,
        restart=restart,
    )
