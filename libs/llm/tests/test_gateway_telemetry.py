"""Every model call through the gateway is a gen_ai span and is counted: tokens, cost, waits."""

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from manifest_llm.embeddings import HashingEmbedder
from manifest_llm.errors import OutputValidationError, RateLimitedError
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway
from manifest_llm.ratelimit import Limits
from manifest_telemetry import stage
from manifest_telemetry.genai import CAPTURE_CONTENT
from manifest_telemetry.testing import Capture, capture

LIMITS = Limits(requests_per_minute=60, tokens_per_minute=100_000)


class Answer(BaseModel):
    total: int


class Bucket:
    def __init__(self, *waits: float) -> None:
        self.waits = list(waits)

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        return self.waits.pop(0) if self.waits else 0.0

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None:
        return None


class Provider:
    name = "anthropic"

    def __init__(self, data: Any = None, error: Exception | None = None) -> None:
        self.data = {"total": 3} if data is None else data
        self.error = error

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        if self.error is not None:
            raise self.error
        return JsonResponse(
            self.data, 1_000, 200, finish_reason="end_turn", response_model=f"{request.model}-x"
        )


@pytest.fixture
def telemetry() -> Capture:
    captured = capture()
    captured.clear_spans()
    return captured


def model() -> str:
    """A model name no other test uses, so cumulative metrics start at zero for it."""
    return f"claude-sonnet-5-5-{uuid.uuid4().hex[:8]}"


def gateway(provider: Provider, *waits: float) -> LlmGateway:
    return LlmGateway(
        {provider.name: provider},
        Bucket(*waits),
        default_limits=LIMITS,
        embedders={"hashing": HashingEmbedder()},
        sleep=lambda _: None,
    )


def ask(llm: LlmGateway, name: str) -> Any:
    return llm.structured(
        "anthropic", name, system="Be exact.", prompt="Total?", output=Answer, max_tokens=64
    )


def test_a_call_is_one_client_span_with_gen_ai_attributes(telemetry: Capture) -> None:
    name = model()
    ask(gateway(Provider()), name)
    [span] = telemetry.spans(f"chat {name}")
    attributes = dict(span.attributes or {})
    assert span.kind.name == "CLIENT"
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.provider.name"] == "anthropic"
    assert attributes["gen_ai.request.model"] == name
    assert attributes["gen_ai.response.model"] == f"{name}-x"
    assert attributes["gen_ai.request.max_tokens"] == 64
    assert attributes["gen_ai.output.type"] == "json"
    assert attributes["gen_ai.usage.input_tokens"] == 1_000
    assert attributes["gen_ai.usage.output_tokens"] == 200
    assert attributes["gen_ai.response.finish_reasons"] == ("end_turn",)
    # Content capture is off by default: no prompt or answer leaves the process.
    assert not any(key.endswith("messages") or "instructions" in key for key in attributes)


def test_content_is_captured_only_when_asked(
    telemetry: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CAPTURE_CONTENT, "true")
    name = model()
    ask(gateway(Provider()), name)
    [span] = telemetry.spans(f"chat {name}")
    attributes = dict(span.attributes or {})
    assert "Be exact." in str(attributes["gen_ai.system_instructions"])
    assert "Total?" in str(attributes["gen_ai.input.messages"])
    assert '\\"total\\": 3' in str(attributes["gen_ai.output.messages"])


def test_tokens_and_cost_are_counted_by_model_and_stage(telemetry: Capture) -> None:
    name = model()
    with stage("page_extract"):
        ask(gateway(Provider()), name)
    dims = {"model": name, "stage": "page_extract", "provider": "anthropic"}
    assert telemetry.total("llm_tokens", {**dims, "type": "input"}) == 1_000
    assert telemetry.total("llm_tokens", {**dims, "type": "output"}) == 200
    # Unknown to the price list (a suffixed name): no cost rather than a guess.
    assert telemetry.total("cost_usd", dims) == 0
    with stage("hs_classify"):
        ask(gateway(Provider()), "claude-sonnet-5-5")
    priced = {"model": "claude-sonnet-5-5", "stage": "hs_classify"}
    assert telemetry.total("cost_usd", priced) >= (1_000 * 2 + 200 * 10) / 1_000_000


def test_rate_limiter_waits_are_recorded_by_outcome(telemetry: Capture) -> None:
    name = model()
    ask(gateway(Provider()), name)  # admitted at once
    ask(gateway(Provider(), 0.5), name)  # slept inline, then admitted
    with pytest.raises(RateLimitedError):
        ask(gateway(Provider(), 30.0), name)  # deferred: the task re-queues
    held = {"model": name}
    assert telemetry.count("rate_limiter_wait", {**held, "outcome": "admitted"}) == 1
    assert telemetry.total("rate_limiter_wait", {**held, "outcome": "waited"}) == 0.5
    assert telemetry.total("rate_limiter_wait", {**held, "outcome": "deferred"}) == 30.0
    # A deferred call never reached the model.
    assert len(telemetry.spans(f"chat {name}")) == 2


def test_a_refusal_is_an_error_span_with_its_finish_reason(telemetry: Capture) -> None:
    name = model()
    refused = OutputValidationError("stopped with refusal", finish_reason="refusal")
    with pytest.raises(OutputValidationError):
        ask(gateway(Provider(error=refused)), name)
    [span] = telemetry.spans(f"chat {name}")
    attributes = dict(span.attributes or {})
    assert attributes["gen_ai.response.finish_reasons"] == ("refusal",)
    assert attributes["error.type"] == "OutputValidationError"
    assert span.status.status_code.name == "ERROR"
    duration = {"gen_ai.request.model": name, "error.type": "OutputValidationError"}
    assert telemetry.count("gen_ai.client.operation.duration", duration) == 1


def test_an_answer_that_fails_validation_still_counts_its_tokens(telemetry: Capture) -> None:
    name = model()
    with pytest.raises(OutputValidationError):
        ask(gateway(Provider(data={"total": "lots"})), name)
    assert telemetry.total("llm_tokens", {"model": name, "type": "input"}) == 1_000


def test_embeddings_are_spans_too(telemetry: Capture) -> None:
    llm = gateway(Provider())
    with stage("hs_classify"):
        llm.embed("hashing", "hashing-v1", ["cotton t-shirts"], input_type="query", dimensions=1024)
    [span] = telemetry.spans("embeddings hashing-v1")
    attributes = dict(span.attributes or {})
    assert attributes["gen_ai.operation.name"] == "embeddings"
    assert attributes["gen_ai.usage.input_tokens"] == 3  # cotton, t, shirts
    assert "gen_ai.usage.output_tokens" not in attributes
    assert telemetry.total("llm_tokens", {"model": "hashing-v1", "stage": "hs_classify"}) >= 3
