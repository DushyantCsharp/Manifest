"""Helpers for tests that need a real database: throwaway databases and Alembic configs.

Synchronous on purpose: Alembic's env.py runs its own event loop, so migrations must be driven
from outside one (async tests use ``asyncio.to_thread``).
"""

import asyncio
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import Row, make_url, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from manifest_db.settings import DatabaseSettings

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    """An Alembic config for ``url`` that needs no alembic.ini (and configures no logging)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["url"] = url
    return config


async def _run(url: str, statement: str, params: dict[str, Any], *, autocommit: bool) -> Any:
    engine = create_async_engine(
        url,
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT" if autocommit else "READ COMMITTED",
    )
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return result.all() if result.returns_rows else None
    finally:
        await engine.dispose()


def execute(url: str, statement: str, **params: Any) -> None:
    asyncio.run(_run(url, statement, params, autocommit=False))


def query(url: str, statement: str, **params: Any) -> Sequence[Row[Any]]:
    rows: Sequence[Row[Any]] = asyncio.run(_run(url, statement, params, autocommit=False))
    return rows


@contextmanager
def scratch_database(server_url: str | None = None) -> Iterator[str]:
    """Create an empty database on the server behind ``server_url``; drop it afterwards."""
    server = make_url(server_url or DatabaseSettings().url)
    name = f"manifest_test_{secrets.token_hex(4)}"
    admin = server.render_as_string(hide_password=False)
    asyncio.run(_run(admin, f'CREATE DATABASE "{name}"', {}, autocommit=True))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        statement = f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
        asyncio.run(_run(admin, statement, {}, autocommit=True))
