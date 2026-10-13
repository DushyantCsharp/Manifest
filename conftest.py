"""Shared test infrastructure: real Postgres, RabbitMQ, Redis and MinIO via testcontainers.

Containers are session-scoped and lazy (started only when a test asks for one), use the same
images as infra/compose.yaml, and need only Docker, not `make up`. Unit tests use none.

CI runs the same images as service containers instead (.github/workflows/ci.yml) and points
each fixture at one through ``MANIFEST_TEST_*`` (see ``EXTERNAL``). Tests still isolate
themselves (their own database, Redis namespace, vhost and object keys), so sharing a server
is safe. Never point these at a server whose data matters.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from hypothesis import settings

# Hypothesis: no per-example deadline (Decimal brute-force oracles are slow on a cold start).
# CI sets HYPOTHESIS_PROFILE=ci for more examples, derandomised so a failure reproduces.
settings.register_profile("dev", deadline=None)
settings.register_profile("ci", deadline=None, max_examples=500, derandomize=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

POSTGRES_IMAGE = "pgvector/pgvector:0.8.7-pg16"
RABBITMQ_IMAGE = "rabbitmq:4.3.6-management"
REDIS_IMAGE = "redis:8.8"
MINIO_IMAGE = "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"
BUCKET = "manifest-documents"

# A running server per fixture, instead of a container: all of a fixture's variables, or none.
EXTERNAL = {
    "postgres_url": ["MANIFEST_TEST_POSTGRES_URL"],  # postgresql+asyncpg://manifest:manifest@…
    "redis_url": ["MANIFEST_TEST_REDIS_URL"],  # redis://host:port/0
    "broker": ["MANIFEST_TEST_AMQP_URL", "MANIFEST_TEST_RABBITMQ_API"],  # user manifest/manifest
    "object_store": ["MANIFEST_TEST_S3_ENDPOINT"],  # MinIO, user manifest/manifest-secret
}


def external(fixture: str) -> list[str] | None:
    """The configured servers' addresses for ``fixture``; None to start a container."""
    names = EXTERNAL[fixture]
    values = [os.environ.get(name, "") for name in names]
    if not any(values):
        return None
    if not all(values):
        raise pytest.UsageError(f"{fixture} needs all of {', '.join(names)} or none")
    return values


@dataclass(frozen=True)
class Broker:
    url: str  # amqp://manifest:manifest@host:port/
    management: str  # http://host:port/api


@dataclass(frozen=True)
class ObjectStore:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    if configured := external("postgres_url"):
        yield configured[0]
        return
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username="manifest", password="manifest", dbname="manifest"
    )
    with container:
        yield container.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    if configured := external("redis_url"):
        yield configured[0]
        return
    from testcontainers.community.redis import RedisContainer

    with RedisContainer(REDIS_IMAGE) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def broker() -> Iterator[Broker]:
    if configured := external("broker"):
        amqp_url, management = configured
        yield Broker(amqp_url.rstrip("/") + "/", management.rstrip("/"))
        return
    from testcontainers.community.rabbitmq import RabbitMqContainer

    container = RabbitMqContainer(RABBITMQ_IMAGE, username="manifest", password="manifest")
    container.with_exposed_ports(5672, 15672)
    with container:
        host = container.get_container_host_ip()
        amqp, http = container.get_exposed_port(5672), container.get_exposed_port(15672)
        yield Broker(f"amqp://manifest:manifest@{host}:{amqp}/", f"http://{host}:{http}/api")


@pytest.fixture(scope="session")
def object_store() -> Iterator[ObjectStore]:
    if configured := external("object_store"):
        from minio import Minio

        endpoint = configured[0].rstrip("/")
        scheme, _, host = endpoint.partition("://")
        client = Minio(host, "manifest", "manifest-secret", secure=scheme == "https")
        if not client.bucket_exists(BUCKET):
            client.make_bucket(BUCKET)
        yield ObjectStore(endpoint, "manifest", "manifest-secret", BUCKET)
        return
    from testcontainers.community.minio import MinioContainer

    container = MinioContainer(MINIO_IMAGE, access_key="manifest", secret_key="manifest-secret")
    with container:
        container.get_client().make_bucket(BUCKET)
        endpoint = f"http://{container.get_config()['endpoint']}"
        yield ObjectStore(endpoint, "manifest", "manifest-secret", BUCKET)
