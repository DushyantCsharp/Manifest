"""OpenTelemetry for every Manifest process: traces and metrics over OTLP to the collector.

- ``configure(service)`` installs the tracer and meter providers once per process (OTLP over
  HTTP to ``OTEL_EXPORTER_OTLP_ENDPOINT``, default ``http://localhost:4318``); ``OTEL_SDK_DISABLED
  =true`` turns it off. ``instrument()`` traces SQLAlchemy, Celery, botocore, redis and httpx;
  ``instrument_fastapi(app)`` the API.
- ``genai`` makes one ``gen_ai.*`` span per model call (semantic conventions for generative
  AI); prompts and answers are captured only with
  ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true``.
- ``metrics`` names every instrument the dashboards and alerts read, in one place.
- ``stage(name)`` says which pipeline stage the code below is working for, so model usage and
  cost can be broken down by stage.
"""

from manifest_telemetry.context import current_stage, stage
from manifest_telemetry.setup import configure, instrument, instrument_fastapi, shutdown

__all__ = [
    "configure",
    "current_stage",
    "instrument",
    "instrument_fastapi",
    "shutdown",
    "stage",
]
