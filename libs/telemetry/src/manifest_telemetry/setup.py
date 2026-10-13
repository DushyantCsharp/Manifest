"""What ``configure`` installs: tracer and meter providers exporting OTLP, and instrumentation.

Standard ``OTEL_*`` variables apply: ``OTEL_SDK_DISABLED``, ``OTEL_EXPORTER_OTLP_ENDPOINT``
(default ``http://localhost:4318``, OTLP over HTTP), ``OTEL_TRACES_SAMPLER``,
``OTEL_RESOURCE_ATTRIBUTES``, ``OTEL_METRIC_EXPORT_INTERVAL`` (milliseconds; 15 s here rather
than the SDK's 60 s, to match Prometheus' scrape interval). ``MANIFEST_ENVIRONMENT`` names the
deployment (default ``local``).

Providers are global and can be set once per process. Celery's prefork pool forks its children
from a parent that runs no tasks, so the worker configures each child (``worker_process_init``);
every process gets its own ``service.instance.id``, so their counters never collide.
"""

import os
import uuid
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DEFAULT_EXPORT_INTERVAL_MS = 15_000
NAMESPACE = "manifest"

_providers: tuple[TracerProvider, MeterProvider] | None = None


def enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get("OTEL_SDK_DISABLED", "").strip().lower() != "true"


def resource(service: str, environ: Mapping[str, str] = os.environ) -> Resource:
    try:
        service_version = version(service)
    except PackageNotFoundError:
        service_version = "unknown"
    return Resource.create(
        {
            "service.name": service,
            "service.namespace": NAMESPACE,
            "service.version": service_version,
            "service.instance.id": str(uuid.uuid4()),
            "deployment.environment.name": environ.get("MANIFEST_ENVIRONMENT", "local"),
        }
    )


def configure(
    service: str,
    *,
    span_processor: SpanProcessor | None = None,
    metric_reader: MetricReader | None = None,
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Install tracing and metrics for this process; False when ``OTEL_SDK_DISABLED=true``.

    Exports OTLP unless given a ``span_processor`` and ``metric_reader`` (tests). Idempotent:
    a second call keeps the first configuration.
    """
    global _providers
    if not enabled(environ):
        return False
    if _providers is not None:
        return True
    attributes = resource(service, environ)
    tracer_provider = TracerProvider(resource=attributes)
    tracer_provider.add_span_processor(span_processor or BatchSpanProcessor(OTLPSpanExporter()))
    interval = int(environ.get("OTEL_METRIC_EXPORT_INTERVAL", DEFAULT_EXPORT_INTERVAL_MS))
    reader = metric_reader or PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=interval
    )
    meter_provider = MeterProvider(resource=attributes, metric_readers=[reader])
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    _providers = (tracer_provider, meter_provider)
    return True


def shutdown() -> None:
    """Flush what is buffered (a worker child's exit skips ``atexit``)."""
    if _providers is None:
        return
    tracer_provider, meter_provider = _providers
    tracer_provider.force_flush()
    meter_provider.force_flush()


def instrument() -> None:
    """Trace SQLAlchemy, Celery, botocore, redis and httpx in this process (idempotent).

    SQLAlchemy is traced by patching ``sqlalchemy.create_engine`` (and the async variant):
    only engines created afterwards are traced, so call this before building sessions.
    """
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    sqlalchemy = SQLAlchemyInstrumentor()
    if not sqlalchemy.is_instrumented_by_opentelemetry:
        # 0.66b1 declares sqlalchemy < 2.1 and otherwise silently instruments nothing; its
        # engine events work on 2.1 (the worker's trace test asserts the database spans).
        sqlalchemy.instrument(skip_dep_check=True)
    instrumentors: list[Any] = [
        CeleryInstrumentor(),  # type: ignore[no-untyped-call]
        BotocoreInstrumentor(),  # type: ignore[no-untyped-call]
        RedisInstrumentor(),
        HTTPXClientInstrumentor(),
    ]
    for instrumentor in instrumentors:
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()


def instrument_fastapi(app: Any) -> None:
    """Server spans for every request but the health check, without per-message ASGI spans."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz", exclude_spans=["receive", "send"]
    )
