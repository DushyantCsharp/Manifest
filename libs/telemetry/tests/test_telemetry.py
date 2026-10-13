"""configure(), stages and gauges: the plumbing every service shares."""

import uuid

import pytest

from manifest_telemetry import current_stage, setup, stage
from manifest_telemetry.genai import capture_content
from manifest_telemetry.metrics import gauge
from manifest_telemetry.testing import Capture, capture


@pytest.fixture
def telemetry() -> Capture:
    return capture()


def test_stages_nest_and_unwind() -> None:
    assert current_stage() == "none"
    with stage("classify"):
        assert current_stage() == "classify"
        with stage("page_extract"):
            assert current_stage() == "page_extract"
        assert current_stage() == "classify"
    assert current_stage() == "none"


def test_disabled_by_the_standard_variable() -> None:
    assert setup.configure("manifest-api", environ={"OTEL_SDK_DISABLED": "true"}) is False
    assert not setup.enabled({"OTEL_SDK_DISABLED": "TRUE"})
    assert setup.enabled({})


def test_configure_is_once_per_process(telemetry: Capture) -> None:
    # The capture configured this process already; a later call keeps it.
    assert setup.configure("manifest-api", environ={}) is True


def test_every_process_is_its_own_instance() -> None:
    first, second = setup.resource("manifest-api", {}), setup.resource("manifest-api", {})
    assert first.attributes["service.name"] == "manifest-api"
    assert first.attributes["service.namespace"] == "manifest"
    assert first.attributes["service.version"] != "unknown"  # read from the installed package
    assert first.attributes["deployment.environment.name"] == "local"
    assert first.attributes["service.instance.id"] != second.attributes["service.instance.id"]
    assert setup.resource("no-such-package", {}).attributes["service.version"] == "unknown"
    staging = {"MANIFEST_ENVIRONMENT": "staging"}
    assert setup.resource("x", staging).attributes["deployment.environment.name"] == "staging"


def test_gauges_report_what_the_sampler_sees(telemetry: Capture) -> None:
    name = f"test_gauge_{uuid.uuid4().hex[:8]}"
    readings = [(3.0, {"queue": "classify"}), (1.0, {"queue": "page_extract"})]
    gauge(name, lambda: readings)
    assert telemetry.total(name, {"queue": "classify"}) == 3.0
    readings[:] = [(0.0, {"queue": "classify"})]
    assert telemetry.total(name, {"queue": "classify"}) == 0.0


def test_content_capture_is_opt_in() -> None:
    assert not capture_content({})
    assert not capture_content({"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false"})
    assert capture_content({"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "True"})


def test_instrumenting_twice_is_harmless() -> None:
    setup.instrument()
    setup.instrument()
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    assert CeleryInstrumentor().is_instrumented_by_opentelemetry  # type: ignore[no-untyped-call]
