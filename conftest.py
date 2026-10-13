"""Shared test infrastructure: real Postgres, RabbitMQ, Redis and MinIO via testcontainers.

Containers are session-scoped and lazy (started only when a test asks for one), use the same
images as infra/compose.yaml, and need only Docker, not `make up`. Unit tests use none.
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
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username="manifest", password="manifest", dbname="manifest"
    )
    with container:
        yield container.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    from testcontainers.community.redis import RedisContainer

    with RedisContainer(REDIS_IMAGE) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def broker() -> Iterator[Broker]:
    from testcontainers.community.rabbitmq import RabbitMqContainer

    container = RabbitMqContainer(RABBITMQ_IMAGE, username="manifest", password="manifest")
    container.with_exposed_ports(5672, 15672)
    with container:
        host = container.get_container_host_ip()
        amqp, http = container.get_exposed_port(5672), container.get_exposed_port(15672)
        yield Broker(f"amqp://manifest:manifest@{host}:{amqp}/", f"http://{host}:{http}/api")


@pytest.fixture(scope="session")
def object_store() -> Iterator[ObjectStore]:
    from testcontainers.community.minio import MinioContainer

    container = MinioContainer(MINIO_IMAGE, access_key="manifest", secret_key="manifest-secret")
    with container:
        container.get_client().make_bucket(BUCKET)
        endpoint = f"http://{container.get_config()['endpoint']}"
        yield ObjectStore(endpoint, "manifest", "manifest-secret", BUCKET)
