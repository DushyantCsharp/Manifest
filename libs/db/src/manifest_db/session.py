"""Synchronous sessions for Celery workers (psycopg); the API uses the async driver (asyncpg).

Both read the same ``MANIFEST_DB_URL``; the driver is swapped here so there is one setting.
"""

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.settings import DatabaseSettings


def sync_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def sync_session_factory(url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(sync_url(url or DatabaseSettings().url), pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)
