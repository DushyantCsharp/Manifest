"""The country-of-origin backfill, and the expand/backfill/contract sequence it unblocks."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

from manifest_db.models import (
    BackfillRun,
    BackfillStatus,
    Document,
    Extraction,
    LineItem,
    Shipment,
)
from manifest_db.testing import alembic_config, scratch_database
from manifest_worker.backfills import country_of_origin, schema_v3
from manifest_worker.backfills import reextract as runner
from manifest_worker.backfills.country_of_origin import BackfillLockedError, Outcome
from manifest_worker.backfills.ratelimit import TokenBucket

# ---------------------------------------------------------------- rate limiting (no database)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_token_bucket_paces_to_the_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2, burst=1, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        await bucket.acquire()
    assert clock.now == pytest.approx(2.0)  # first is free, then one every 0.5 s


async def test_token_bucket_allows_a_burst_then_paces() -> None:
    clock = FakeClock()
    bucket = TokenBucket(1, burst=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert clock.now == 0
    await bucket.acquire()
    assert clock.now == pytest.approx(1.0)


def test_token_bucket_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="rate must be positive"):
        TokenBucket(0)


# ---------------------------------------------------------------- against Postgres


def unlimited() -> TokenBucket:
    return TokenBucket(1_000_000, burst=1_000)


@pytest.fixture
def database(postgres_url: str) -> Iterator[str]:
    """Migrated to 0004: v2 code is live, the contract (0005) hasn't been applied yet."""
    with scratch_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "0004")
        yield url


@pytest.fixture
async def engine(database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database)
    yield engine
    await engine.dispose()


# Rows are written column by column, never as ORM objects: these tests run at revision 0004,
# and the models describe head, whose newer columns don't exist yet.
async def add_document(
    engine: AsyncEngine,
    *,
    schema_version: int = 1,
    origins: Sequence[str | None] = (None, None),
) -> UUID:
    async with engine.begin() as connection:
        shipment_id = await connection.scalar(
            insert(Shipment).values(reference="backfill test").returning(Shipment.id)
        )
        document_id = await connection.scalar(
            insert(Document)
            .values(
                shipment_id=shipment_id,
                filename="invoice.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                storage_key=f"raw/{uuid4()}.pdf",
            )
            .returning(Document.id)
        )
        assert document_id is not None
        await _add_extraction(connection, document_id, schema_version, origins)
        return document_id


async def _add_extraction(
    connection: AsyncConnection,
    document_id: UUID,
    schema_version: int,
    origins: Sequence[str | None],
) -> None:
    extraction_id = await connection.scalar(
        insert(Extraction)
        .values(
            document_id=document_id,
            schema_version=schema_version,
            payload={},
            model="test-model",
            tokens_in=100,
            tokens_out=50,
        )
        .returning(Extraction.id)
    )
    await connection.execute(
        insert(LineItem),
        [
            {
                "extraction_id": extraction_id,
                "line_no": number,
                "description": "Laptop computers",
                "country_of_origin": origin,
            }
            for number, origin in enumerate(origins, start=1)
        ],
    )


async def reextract(engine: AsyncEngine, document_id: UUID, origin: str | None) -> None:
    """What the real re-extract task must do, including the idempotency it promises."""
    async with engine.begin() as connection:
        current = (
            await connection.execute(
                select(Extraction.id, Extraction.schema_version).where(
                    Extraction.document_id == document_id, Extraction.superseded_at.is_(None)
                )
            )
        ).one()
        if current.schema_version >= country_of_origin.TO_SCHEMA_VERSION:
            return
        await connection.execute(
            update(Extraction).where(Extraction.id == current.id).values(superseded_at=func.now())
        )
        await _add_extraction(connection, document_id, 2, (origin, origin))


async def backfill_row(engine: AsyncEngine) -> BackfillRun:
    async with AsyncSession(engine) as session:
        row = await session.get(BackfillRun, country_of_origin.NAME)
        assert row is not None
        return row


class Recorder:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def __call__(self, document_id: UUID) -> None:
        self.enqueued.append(document_id)


@pytest.mark.integration
async def test_selects_only_current_v1_documents_missing_an_origin(engine: AsyncEngine) -> None:
    targets = [await add_document(engine) for _ in range(3)]
    targets.append(await add_document(engine, origins=("CN", None)))  # one line still missing
    await add_document(engine, origins=("CN", "DE"))  # complete already
    await add_document(engine, schema_version=2, origins=(None, None))  # v2: NULL is a finding
    replaced = await add_document(engine)
    await reextract(engine, replaced, "VN")  # superseded by a v2 extraction

    recorder = Recorder()
    outcome = await country_of_origin.run(engine, recorder, limiter=unlimited(), batch_size=2)

    assert recorder.enqueued == sorted(targets)
    assert outcome == Outcome(enqueued=4, batches=2, completed=True)
    row = await backfill_row(engine)
    assert (row.status, row.cursor, row.processed) == (BackfillStatus.COMPLETED, None, 4)


@pytest.mark.integration
async def test_interrupted_pass_resumes_from_its_checkpoint(engine: AsyncEngine) -> None:
    targets = sorted([await add_document(engine) for _ in range(5)])

    first = Recorder()
    outcome = await country_of_origin.run(
        engine, first, limiter=unlimited(), batch_size=2, max_batches=1
    )
    assert outcome == Outcome(enqueued=2, batches=1, completed=False)
    row = await backfill_row(engine)
    assert (row.status, row.cursor, row.processed) == (BackfillStatus.RUNNING, str(targets[1]), 2)

    second = Recorder()
    await country_of_origin.run(engine, second, limiter=unlimited(), batch_size=2)
    assert first.enqueued + second.enqueued == targets
    assert (await backfill_row(engine)).processed == 5


@pytest.mark.integration
async def test_restart_discards_the_cursor(engine: AsyncEngine) -> None:
    targets = sorted([await add_document(engine) for _ in range(3)])
    await country_of_origin.run(
        engine, Recorder(), limiter=unlimited(), batch_size=1, max_batches=2
    )

    again = Recorder()
    await country_of_origin.run(engine, again, limiter=unlimited(), batch_size=1, restart=True)
    assert again.enqueued == targets


@pytest.mark.integration
async def test_only_one_runner_at_a_time(engine: AsyncEngine) -> None:
    async with engine.connect() as other:
        await other.execute(
            text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": country_of_origin.NAME}
        )
        with pytest.raises(BackfillLockedError):
            await country_of_origin.run(engine, Recorder(), limiter=unlimited())
        await other.execute(
            text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": country_of_origin.NAME}
        )


@pytest.mark.integration
async def test_expand_backfill_contract(database: str, engine: AsyncEngine) -> None:
    config = alembic_config(database)
    stated, unstated = await add_document(engine), await add_document(engine)

    with pytest.raises(RuntimeError, match="2 v1 document"):
        await asyncio.to_thread(command.upgrade, config, "0005")

    origins = {stated: "VN", unstated: None}  # some documents never state an origin

    async def reextract_now(document_id: UUID) -> None:
        await reextract(engine, document_id, origins[document_id])

    outcome = await country_of_origin.run(engine, reextract_now, limiter=unlimited())
    assert outcome.enqueued == 2

    await asyncio.to_thread(command.upgrade, config, "0005")  # contract now applies

    # A second pass finds nothing left; a redelivered task would be a no-op.
    assert (await country_of_origin.run(engine, reextract_now, limiter=unlimited())).enqueued == 0
    await reextract_now(stated)


@pytest.fixture
def at_0007(postgres_url: str) -> Iterator[str]:
    """Migrated to 0007: v3 code is live, the v3 contract (0008) hasn't been applied yet."""
    with scratch_database(postgres_url) as url:
        command.upgrade(alembic_config(url), "0007")
        yield url


@pytest.mark.integration
async def test_the_v3_backfill_selects_documents_older_than_v3(at_0007: str) -> None:
    engine = create_async_engine(at_0007)
    try:
        older = [
            await add_document(engine, schema_version=1),
            await add_document(engine, schema_version=2, origins=("CN",)),
        ]
        current = await add_document(engine, schema_version=2, origins=("CN",))
        async with engine.begin() as connection:  # re-extracted: v2 superseded by v3
            await connection.execute(
                update(Extraction)
                .where(Extraction.document_id == current)
                .values(superseded_at=func.now())
            )
            await connection.execute(
                insert(Extraction).values(
                    document_id=current,
                    schema_version=3,
                    payload={},
                    provenance={},
                    model="test-model",
                    tokens_in=1,
                    tokens_out=1,
                )
            )
        recorder = Recorder()
        outcome = await runner.run(
            schema_v3.SPEC, engine, recorder, limiter=unlimited(), batch_size=10
        )
        assert outcome.completed
        assert sorted(recorder.enqueued) == sorted(older)
    finally:
        await engine.dispose()
