"""Worker test fixtures on top of the repo-wide containers (see the root conftest.py)."""

import secrets
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from manifest_worker.app import celery_app
from manifest_worker.topology import declare


@pytest.fixture
def vhost(broker: Any) -> Iterator[str]:
    """A fresh RabbitMQ vhost: no queue, delayed message or dead letter leaks between tests."""
    name = f"manifest_test_{secrets.token_hex(4)}"
    auth = ("manifest", "manifest")
    httpx.put(f"{broker.management}/vhosts/{name}", auth=auth).raise_for_status()
    permissions = {"configure": ".*", "write": ".*", "read": ".*"}
    httpx.put(f"{broker.management}/permissions/{name}/manifest", json=permissions, auth=auth)
    yield f"{broker.url}{name}"
    httpx.delete(f"{broker.management}/vhosts/{name}", auth=auth)


def _forget_connections() -> None:
    """Drop pooled broker connections, so a new ``broker_url`` takes effect."""
    pool = celery_app._pool
    if pool is not None:
        pool.force_close_all()
    celery_app._pool = None
    amqp = celery_app.__dict__.get("amqp")
    if amqp is not None:
        amqp._producer_pool = None


@pytest.fixture
def celery_broker(vhost: str, redis_url: str) -> Iterator[str]:
    """The Celery app on a fresh vhost with our topology declared, holding no connection to
    an earlier test's (deleted) vhost. Yields the broker URL.
    """
    _forget_connections()
    celery_app.conf.update(broker_url=vhost, result_backend=redis_url.replace("/0", "/1"))
    with celery_app.connection_for_write() as connection:
        declare(connection)
    yield vhost
    _forget_connections()


def _wait_for(condition: Callable[[], bool], timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("pipeline did not settle in time")
        time.sleep(0.5)


@pytest.fixture
def wait_for() -> Callable[..., None]:
    """Poll ``condition`` until it holds (fails the test after ``timeout`` seconds)."""
    return _wait_for
