"""Sessions: synchronous for Celery workers (psycopg), asynchronous for the API (asyncpg).

Both read the same ``MANIFEST_DB_URL``; the driver is swapped here so there is one setting.
Engines are created through the module attributes (``sqlalchemy.create_engine``), not names
imported at load time, so OpenTelemetry's SQLAlchemy instrumentation (which patches those
attributes) traces them.
"""

import sqlalchemy
from sqlalchemy import make_url
from sqlalchemy.ext import asyncio as sqlalchemy_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.settings import DatabaseSettings


def sync_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def async_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def sync_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = sqlalchemy.create_engine(sync_url(url or DatabaseSettings().url), pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)


def async_session_factory(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    engine = sqlalchemy_asyncio.create_async_engine(
        async_url(url or DatabaseSettings().url), pool_pre_ping=True
    )
    return async_sessionmaker(engine, expire_on_commit=False)
