"""Re-extraction backfills: re-run documents through a newer extraction schema, out of band.

A backfill is a selection of documents (``Spec.candidates``) and a target schema version. The
runner checkpoints in ``backfill_runs`` (registered by the migration that needs it):

- candidates come in ``document_id`` order, so an interrupted pass resumes at its cursor; a
  finished pass clears the cursor and marks the run completed;
- one runner at a time: a Postgres advisory lock named after the backfill;
- enqueueing is paced by a token bucket, so a backfill can't flood the pipeline;
- a checkpoint after every batch. A crash between enqueue and checkpoint re-enqueues that
  batch, which is why the re-extract task is idempotent (a no-op once a document is current).
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from manifest_db.models import BackfillRun, BackfillStatus
from manifest_worker.backfills.ratelimit import TokenBucket
from manifest_worker.tasks.classify import reextract_document

Enqueue = Callable[[UUID], Awaitable[None]]
Candidates = Callable[[UUID | None, int], Select[tuple[UUID]]]

runs = BackfillRun.__table__


class BackfillLockedError(RuntimeError):
    """Another runner holds this backfill's advisory lock."""


@dataclass(frozen=True)
class Outcome:
    enqueued: int
    batches: int
    completed: bool


@dataclass(frozen=True)
class Spec:
    name: str  # its row in backfill_runs
    to_schema_version: int
    candidates: Candidates  # document ids after the cursor, in order


async def _start(engine: AsyncEngine, name: str, *, restart: bool) -> UUID | None:
    """Mark the run as running and return the cursor to resume from."""
    async with engine.begin() as connection:
        row = (await connection.execute(select(runs.c.cursor).where(runs.c.name == name))).first()
        if row is None:
            raise RuntimeError(f"backfill {name!r} is not registered; run `make migrate` first")
        values: dict[str, object] = {
            "status": BackfillStatus.RUNNING,
            "started_at": func.coalesce(runs.c.started_at, func.now()),
            "updated_at": func.now(),
        }
        if restart:
            values |= {"cursor": None, "processed": 0, "started_at": func.now()}
        await connection.execute(update(runs).where(runs.c.name == name).values(values))
    return None if restart or row.cursor is None else UUID(row.cursor)


async def _next_batch(
    engine: AsyncEngine, spec: Spec, after: UUID | None, size: int
) -> Sequence[UUID]:
    async with engine.connect() as connection:
        return (await connection.scalars(spec.candidates(after, size))).all()


async def _checkpoint(engine: AsyncEngine, name: str, last: UUID, count: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(runs)
            .where(runs.c.name == name)
            .values(cursor=str(last), processed=runs.c.processed + count, updated_at=func.now())
        )


async def _complete(engine: AsyncEngine, name: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(runs)
            .where(runs.c.name == name)
            .values(
                status=BackfillStatus.COMPLETED,
                cursor=None,
                completed_at=func.now(),
                updated_at=func.now(),
            )
        )


async def run(
    spec: Spec,
    engine: AsyncEngine,
    enqueue: Enqueue,
    *,
    limiter: TokenBucket,
    batch_size: int = 100,
    max_batches: int | None = None,
    restart: bool = False,
) -> Outcome:
    lock_key = func.hashtext(spec.name)
    async with engine.connect() as lock:
        # Autocommit: the lock lives as long as the session, without an hours-long transaction.
        await lock.execution_options(isolation_level="AUTOCOMMIT")
        if not (await lock.execute(select(func.pg_try_advisory_lock(lock_key)))).scalar_one():
            raise BackfillLockedError(f"another runner is already running backfill {spec.name!r}")
        try:
            after = await _start(engine, spec.name, restart=restart)
            enqueued = batches = 0
            while max_batches is None or batches < max_batches:
                batch = await _next_batch(engine, spec, after, batch_size)
                if not batch:
                    await _complete(engine, spec.name)
                    return Outcome(enqueued, batches, completed=True)
                for document_id in batch:
                    await limiter.acquire()
                    await enqueue(document_id)
                after = batch[-1]
                await _checkpoint(engine, spec.name, after, len(batch))
                enqueued += len(batch)
                batches += 1
            return Outcome(enqueued, batches, completed=False)
        finally:
            await lock.execute(select(func.pg_advisory_unlock(lock_key)))


def celery_enqueue(to_schema_version: int) -> Enqueue:
    """Enqueue re-extraction into ``to_schema_version`` (paced by the caller's bucket)."""

    async def enqueue(document_id: UUID) -> None:
        await asyncio.to_thread(reextract_document.delay, str(document_id), to_schema_version)

    return enqueue
