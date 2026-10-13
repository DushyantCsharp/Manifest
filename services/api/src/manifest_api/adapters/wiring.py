"""Assemble the production ports from settings, and close what they hold."""

import dataclasses
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from manifest_api.adapters.idempotency import RedisIdempotencyStore
from manifest_api.adapters.pipeline import CeleryPipeline
from manifest_api.adapters.settings import ApiSettings
from manifest_api.adapters.shipments import PendingPages, PostgresShipments
from manifest_api.adapters.uploads import S3Uploads
from manifest_api.ports import Ports
from manifest_db.session import async_session_factory


def build(
    settings: ApiSettings, *, database_url: str | None = None
) -> tuple[Ports, Callable[[], Awaitable[None]]]:
    """The ports (review stays unwired: 501) and a coroutine that releases their resources.

    The database is ``MANIFEST_DB_URL`` unless ``database_url`` says otherwise. Nothing
    connects here; pools open on first use.
    """
    sessions = async_session_factory(database_url)
    redis = Redis.from_url(settings.redis_url)
    pipeline = CeleryPipeline(settings.broker_url)
    admission = PendingPages(
        sessions,
        high_water=settings.pending_pages_high_water,
        retry_after=settings.admission_retry_after_seconds,
    )
    ports = dataclasses.replace(
        Ports.unwired(),
        shipments=PostgresShipments(sessions, S3Uploads(settings), pipeline, admission),
        admission=admission,
        idempotency=RedisIdempotencyStore(redis, ttl=settings.idempotency_ttl_seconds),
    )

    async def close() -> None:
        pipeline.close()
        await redis.aclose()
        await sessions.kw["bind"].dispose()

    return ports, close
