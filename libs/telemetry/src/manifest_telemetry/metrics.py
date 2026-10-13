"""Every metric the dashboards and alerts read, named in one place.

Instruments are created on the global meter, which forwards to the provider ``configure``
installs (before that, and when telemetry is off, they record nothing). The collector exposes
them to Prometheus with unit suffixes: ``page_latency`` (s) becomes ``page_latency_seconds``,
counters gain ``_total``.

Pipeline (worker):
    pages_processed_total{status, failure, kind}   pages whose extraction settled
    page_latency_seconds{status}                   processing time of the settling attempt
    shipments_routed_total{decision}               auto_approved | needs_review
Models (manifest_llm, every call):
    llm_tokens_total{provider, model, stage, type} type: input | output
    cost_usd_total{provider, model, stage}         at list prices (manifest_llm.pricing)
    rate_limiter_wait_seconds{provider, model, outcome}  admitted | waited | deferred
    gen_ai_client_operation_duration_seconds, gen_ai_client_token_usage (semantic conventions)
System state (the monitor process, sampled every export):
    queue_depth{queue}, queue_unacked{queue}       messages ready / delivered, not yet acked
    dlq_depth{queue}                               parked messages per dead-letter queue
    auto_approve_ratio{window}                     share of shipments routed without review
    review_queue_age_seconds, review_queue_open    oldest open review task; how many are open
"""

from collections.abc import Callable, Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, ObservableGauge, Observation

_meter = metrics.get_meter("manifest")

PAGE_LATENCY_BUCKETS = (0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 300)
WAIT_BUCKETS = (0, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
# From the GenAI semantic conventions.
GENAI_DURATION_BUCKETS = (
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92
)  # fmt: skip
GENAI_TOKEN_BUCKETS = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864
)  # fmt: skip

pages_processed = _meter.create_counter(
    "pages_processed",
    unit="{page}",
    description="Pages whose extraction settled, by status (extracted, failed) and failure.",
)
page_latency = _meter.create_histogram(
    "page_latency",
    unit="s",
    description="Processing time of the attempt that settled a page (text, model call, writes).",
    explicit_bucket_boundaries_advisory=PAGE_LATENCY_BUCKETS,
)
shipments_routed = _meter.create_counter(
    "shipments_routed",
    unit="{shipment}",
    description="Shipments routed after HS classification, by decision.",
)
llm_tokens = _meter.create_counter(
    "llm_tokens",
    unit="{token}",
    description="Model tokens billed, by provider, model, pipeline stage and type.",
)
cost_usd = _meter.create_counter(
    "cost_usd",
    unit="{USD}",
    description="Model spend in US dollars at list prices, by provider, model and stage.",
)
rate_limiter_wait = _meter.create_histogram(
    "rate_limiter_wait",
    unit="s",
    description=(
        "Wait imposed by the shared token bucket per call: 0 when admitted at once, the "
        "in-process sleep when waited, the re-queue countdown when deferred."
    ),
    explicit_bucket_boundaries_advisory=WAIT_BUCKETS,
)
genai_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="GenAI operation duration.",
    explicit_bucket_boundaries_advisory=GENAI_DURATION_BUCKETS,
)
genai_token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Number of input and output tokens used.",
    explicit_bucket_boundaries_advisory=GENAI_TOKEN_BUCKETS,
)

Sampler = Callable[[], Iterable[tuple[float, dict[str, str]]]]


def gauge(name: str, sample: Sampler, *, unit: str = "", description: str = "") -> ObservableGauge:
    """An observable gauge read by ``sample()`` at every export: (value, attributes) pairs.

    A sampler that raises reports nothing this time; the next export tries again.
    """

    def observe(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(value, attributes) for value, attributes in sample()]

    return _meter.create_observable_gauge(
        name, callbacks=[observe], unit=unit, description=description
    )
