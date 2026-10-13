"""Backfill for extraction schema v3: re-extract every document whose current extraction is
older, so each value gains its page, evidence and confidence signals.

Registered by migration 0007; migration 0008 refuses to apply until no current pre-v3
extraction remains (except on documents that failed outright, which nothing can re-read).
Re-extraction reuses each page's stored text, so it repeats the model calls but not OCR.
"""

from uuid import UUID

from sqlalchemy import Select, select

from manifest_db.models import Extraction
from manifest_worker.backfills import reextract

NAME = "extractions.schema_v3"
TO_SCHEMA_VERSION = 3


def candidates(after: UUID | None, limit: int) -> Select[tuple[UUID]]:
    query = (
        select(Extraction.document_id)
        .where(
            Extraction.superseded_at.is_(None),
            Extraction.schema_version < TO_SCHEMA_VERSION,
        )
        .order_by(Extraction.document_id)
        .limit(limit)
    )
    return query if after is None else query.where(Extraction.document_id > after)


SPEC = reextract.Spec(NAME, TO_SCHEMA_VERSION, candidates)
celery_enqueue = reextract.celery_enqueue(TO_SCHEMA_VERSION)
