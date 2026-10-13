"""The Celery app: four stage queues on RabbitMQ, Redis result backend (chords need one)."""

from pathlib import Path
from typing import Literal

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_process_shutdown
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

import manifest_telemetry
from manifest_worker.topology import STAGES, WORK_QUEUES, declare


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MANIFEST_", env_file=".env", extra="ignore")

    broker_url: str = "amqp://manifest:manifest@localhost:5672//"
    result_backend_url: str = "redis://localhost:6379/1"
    redis_url: str = "redis://localhost:6379/0"  # shared rate-limit buckets
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "manifest-documents"
    s3_access_key: str = "manifest"
    s3_secret_key: str = "manifest-secret"  # noqa: S105 - local MinIO default (.env.example)
    s3_region: str = "us-east-1"
    # Transient failures: exponential backoff from this many seconds, with full jitter.
    retry_backoff: int = 2
    retry_backoff_max: int = 60
    max_retries: int = 4
    llm_requests_per_minute: int = 50
    llm_tokens_per_minute: int = 40_000
    llm_max_inline_wait: float = 2.0
    llm_model: str = "claude-sonnet-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    hs_model: str = "claude-sonnet-5-5"
    hs_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "medium"
    # HS retrieval: pgvector over Voyage embeddings (make hs-index); full-text only without a key.
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    embedding_model: str = "voyage-3.5"
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # Replay recorded model answers from this folder instead of (or while) calling the model.
    llm_cassettes: Path | None = None
    llm_cassette_mode: Literal["replay", "record", "new"] = "replay"
    # Make each replay take as long as the recorded call did (load tests: real model latency
    # without calling or paying for the model).
    llm_replay_latency: bool = False
    # Off: lines get no HS suggestion and route to review (HS_UNCERTAIN), without a model call.
    hs_enabled: bool = True
    # The monitor (python -m manifest_worker.monitor): queue depths come from the management
    # API, with the broker URL's credentials and vhost; the auto-approve ratio covers shipments
    # routed in the trailing window.
    rabbitmq_api_url: str = "http://localhost:15672/api"
    auto_approve_window_minutes: int = 60
    ocr_min_chars: int = 20
    # Committed calibrators (manifest_domain/calibrators/<name>-v<N>.json) to score with, e.g.
    # "extraction-v1"; empty: confidences stay raw scores and nothing auto-approves.
    extraction_calibrator: str | None = None
    hs_calibrator: str | None = None


settings = WorkerSettings()

# One module per stage queue.
TASK_MODULES = [f"manifest_worker.tasks.{stage}" for stage in STAGES]

celery_app = Celery(
    "manifest",
    broker=settings.broker_url,
    backend=settings.result_backend_url,
    include=TASK_MODULES,
)
celery_app.conf.update(
    task_queues=WORK_QUEUES,
    task_default_queue="classify",
    task_create_missing_queues=False,  # an unrouted task must fail, not invent a classic queue
    task_routes={
        "manifest.classify": {"queue": "classify"},
        "manifest.reextract_document": {"queue": "classify"},
        "manifest.page_extract": {"queue": "page_extract"},
        "manifest.reconcile": {"queue": "reconcile"},
        "manifest.reconcile_shipment": {"queue": "reconcile"},
        "manifest.hs_classify": {"queue": "hs_classify"},
    },
    # Backpressure: reserve one message at a time and ack only after the task finishes,
    # so a crashed worker's message is redelivered (and counted against the delivery limit).
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={"confirm_publish": True},
    broker_native_delayed_delivery_queue_type="quorum",
    # RabbitMQ 4 refuses transient non-exclusive queues, which Celery's remote-control
    # mailbox (and mingle/gossip) declare; run workers with --without-gossip --without-mingle.
    worker_enable_remote_control=False,
    result_expires=86_400,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    enable_utc=True,
    timezone="UTC",
)


@worker_init.connect
def _declare_topology(sender: object = None, **_: object) -> None:
    # Work queues are declared by Celery; DLQs and the dead-letter exchange are ours.
    with celery_app.connection_for_write() as connection:
        declare(connection)
    # Prefork children configure themselves after the fork (below); a solo or threads pool
    # runs tasks in this process, so it is configured here.
    if "prefork" not in str(getattr(sender, "pool_cls", "prefork")).lower():
        _telemetry()


@worker_process_init.connect
def _telemetry(**_: object) -> None:
    """Traces and metrics for this worker process; spans continue the publisher's trace."""
    if manifest_telemetry.configure("manifest-worker"):
        manifest_telemetry.instrument()


@worker_process_shutdown.connect
def _flush_telemetry(**_: object) -> None:
    manifest_telemetry.shutdown()
