"""Keyset pagination stays stable while other connections write (real Postgres).

Pages are read one request at a time while concurrent connections commit rows at random
positions in the order (before the cursor, after it, tied with it) and take tasks out of the
review queue. Every row that existed when paging began, and was still listed, comes back
exactly once and in order. OFFSET paging under the same writes repeats rows, which shows the
test can see the failure it guards against.
"""

import asyncio
import itertools
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import Select, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from manifest_db.keyset import review_queue_page, review_task_key, shipment_key, shipments_page
from manifest_db.models import ReviewTask, Shipment
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.review import ReviewPriority, ReviewTaskStatus

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, tzinfo=UTC)
BATCHES, PER_BATCH, LIMIT = 6, 8, 7  # rows in a batch share created_at: ties break on id
CONCURRENT_WRITES = 40
MAX_PAGES = 50  # far more than the rows can fill


@pytest.fixture
def database(postgres_url: str) -> Iterator[str]:
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        yield database


@pytest.fixture
async def engine(database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database, pool_size=4)
    yield engine
    await engine.dispose()


def minutes(n: float) -> datetime:
    return T0 + timedelta(minutes=n)


async def add_shipments(engine: AsyncEngine, created: Sequence[datetime]) -> list[UUID]:
    async with engine.begin() as connection:
        rows = await connection.execute(
            insert(Shipment).returning(Shipment.id),
            [{"reference": "test", "created_at": at} for at in created],
        )
        return [row.id for row in rows]


async def add_tasks(
    engine: AsyncEngine, rng: random.Random, created: Sequence[datetime]
) -> list[UUID]:
    shipments = await add_shipments(engine, created)  # one open task per shipment
    async with engine.begin() as connection:
        rows = await connection.execute(
            insert(ReviewTask).returning(ReviewTask.id),
            [
                {
                    "shipment_id": shipment,
                    "priority": rng.choice(list(ReviewPriority)),
                    "created_at": at,
                }
                for shipment, at in zip(shipments, created, strict=True)
            ],
        )
        return [row.id for row in rows]


async def read_pages[K](
    engine: AsyncEngine, page: Callable[[K | None], Select[Any]], key: Callable[[Any], K]
) -> tuple[list[Any], list[int]]:
    """Page to the end, one session per page (as separate API requests would)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    rows: list[Any] = []
    sizes: list[int] = []
    after: K | None = None
    while True:
        async with sessions() as session:
            batch = list((await session.scalars(page(after))).all())
        if not batch:
            return rows, sizes
        assert len(sizes) < MAX_PAGES, "paging never ended: the cursor isn't advancing"
        rows += batch
        sizes.append(len(batch))
        after = key(batch[-1])
        await asyncio.sleep(0.005)  # let the writers in between pages


async def write_concurrently(
    write: Callable[[], Awaitable[object]], stop: asyncio.Event, writes: list[object]
) -> None:
    while not stop.is_set() and len(writes) < CONCURRENT_WRITES:
        writes.append(await write())
        await asyncio.sleep(0)


async def while_writing[T](
    write: Callable[[], Awaitable[object]], read: Awaitable[T]
) -> tuple[T, list[object]]:
    stop, writes = asyncio.Event(), list[object]()
    writer = asyncio.create_task(write_concurrently(write, stop, writes))
    try:
        return await read, writes
    finally:
        stop.set()
        await writer


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_shipments_page_stably_under_concurrent_inserts(
    engine: AsyncEngine, seed: int
) -> None:
    rng = random.Random(seed)
    existing: set[UUID] = set()
    for batch in range(BATCHES):
        existing |= set(await add_shipments(engine, [minutes(batch)] * PER_BATCH))
    inserted: list[UUID] = []

    async def insert_anywhere() -> None:
        # Anywhere in the order: newer than everything, between pages, tied with a batch.
        at = minutes(rng.choice([rng.uniform(-2, BATCHES + 2), rng.randrange(BATCHES)]))
        inserted.extend(await add_shipments(engine, [at] * rng.randint(1, 3)))

    pages = read_pages(engine, lambda after: shipments_page(LIMIT, after), shipment_key)
    (rows, sizes), writes = await while_writing(insert_anywhere, pages)

    ids = [row.id for row in rows]
    assert len(ids) == len(set(ids)), "a row came back twice"
    assert existing <= set(ids), "a row that existed all along was skipped"
    assert set(ids) <= existing | set(inserted)
    keys = [shipment_key(row) for row in rows]
    assert all(a > b for a, b in itertools.pairwise(keys)), "out of order"
    assert all(size == LIMIT for size in sizes[:-1])
    assert writes, "nothing was written while paging"


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_review_queue_pages_stably_while_tasks_arrive_and_leave(
    engine: AsyncEngine, seed: int
) -> None:
    rng = random.Random(seed)
    existing: list[UUID] = []
    for batch in range(BATCHES):
        existing += await add_tasks(engine, rng, [minutes(batch)] * PER_BATCH)
    inserted: list[UUID] = []
    approved: set[UUID] = set()

    async def arrive_or_leave() -> None:
        if rng.random() < 0.3:  # a reviewer approves a task: it leaves the queue
            task = rng.choice(existing)
            async with engine.begin() as connection:
                await connection.execute(
                    update(ReviewTask)
                    .where(ReviewTask.id == task)
                    .values(status=ReviewTaskStatus.APPROVED)
                )
            approved.add(task)
        else:
            created = [minutes(rng.uniform(-2, BATCHES + 2))]
            inserted.extend(await add_tasks(engine, rng, created))

    pages = read_pages(engine, lambda after: review_queue_page(LIMIT, after), review_task_key)
    (rows, _), writes = await while_writing(arrive_or_leave, pages)

    ids = [row.id for row in rows]
    assert len(ids) == len(set(ids)), "a task came back twice"
    assert set(existing) - approved <= set(ids), "a task that stayed queued was skipped"
    assert set(ids) <= set(existing) | set(inserted)
    keys = [review_task_key(row) for row in rows]
    assert all(a < b for a, b in itertools.pairwise(keys)), "out of order"
    assert writes, "nothing was written while paging"


async def test_review_queue_filters(engine: AsyncEngine) -> None:
    await add_tasks(engine, random.Random(7), [minutes(n) for n in range(30)])
    sessions = async_sessionmaker(engine)
    async with sessions() as session:
        everything = (await session.scalars(review_queue_page(100))).all()
        urgent = (
            await session.scalars(review_queue_page(100, priorities=[ReviewPriority.URGENT]))
        ).all()
        in_progress = (
            await session.scalars(review_queue_page(100, statuses=[ReviewTaskStatus.IN_PROGRESS]))
        ).all()
    assert len(everything) == 30
    assert urgent
    assert {task.priority for task in urgent} == {ReviewPriority.URGENT}
    assert in_progress == []


async def test_offset_paging_repeats_rows_under_the_same_inserts(engine: AsyncEngine) -> None:
    """The failure keyset avoids: rows inserted ahead of an OFFSET shift later pages."""
    for batch in range(BATCHES):
        await add_shipments(engine, [minutes(batch)] * PER_BATCH)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    order = (Shipment.created_at.desc(), Shipment.id.desc())

    async def offset_page(offset: int) -> list[UUID]:
        async with sessions() as session:
            query = select(Shipment.id).order_by(*order).offset(offset).limit(LIMIT)
            return list((await session.scalars(query)).all())

    async def keyset_page(after: Any) -> list[Shipment]:
        async with sessions() as session:
            return list((await session.scalars(shipments_page(LIMIT, after))).all())

    first_offset, first_keyset = await offset_page(0), await keyset_page(None)
    await add_shipments(engine, [minutes(BATCHES + 1)] * 3)  # three newer shipments arrive
    second_offset = await offset_page(LIMIT)
    second_keyset = await keyset_page(shipment_key(first_keyset[-1]))

    assert set(first_offset) & set(second_offset)  # OFFSET: the same rows twice
    assert not {s.id for s in first_keyset} & {s.id for s in second_keyset}
