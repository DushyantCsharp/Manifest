"""The gateway (no Redis): admission, inline waits vs re-queue, validation, error mapping."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, Field

from manifest_llm.anthropic_provider import AnthropicProvider, classify, strict_schema
from manifest_llm.errors import (
    OutputValidationError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway, Unlimited, estimate_tokens
from manifest_llm.ratelimit import Limits

LIMITS = Limits(requests_per_minute=60, tokens_per_minute=10_000)


class Answer(BaseModel):
    total: int


class FakeBucket:
    def __init__(self, *waits: float) -> None:
        self.waits = list(waits)
        self.settled: list[int] = []

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        return self.waits.pop(0) if self.waits else 0.0

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None:
        self.settled.append(delta)


class FakeProvider:
    name = "fake"

    def __init__(self, data: Any) -> None:
        self.data = data
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        self.requests.append(request)
        return JsonResponse(self.data, input_tokens=100, output_tokens=20)


def gateway(provider: FakeProvider, bucket: FakeBucket, sleeps: list[float]) -> LlmGateway:
    return LlmGateway(
        {"fake": provider},
        bucket,
        default_limits=LIMITS,
        max_inline_wait=2.0,
        sleep=sleeps.append,
    )


def call(llm: LlmGateway) -> Any:
    return llm.structured("fake", "m", system="s", prompt="p", output=Answer, max_tokens=50)


def test_short_wait_is_slept_inline_and_usage_settled() -> None:
    sleeps: list[float] = []
    bucket, provider = FakeBucket(0.5, 0.0), FakeProvider({"total": 3})
    result = call(gateway(provider, bucket, sleeps))
    assert result.output == Answer(total=3)
    assert sleeps == [0.5]
    assert bucket.settled == [120 - estimate_tokens(provider.requests[0])]


def test_long_wait_raises_for_requeue_without_calling_the_provider() -> None:
    provider = FakeProvider({"total": 3})
    with pytest.raises(RateLimitedError) as caught:
        call(gateway(provider, FakeBucket(7.5), []))
    assert caught.value.retry_after == 7.5
    assert provider.requests == []


def test_invalid_output_is_a_validation_error() -> None:
    with pytest.raises(OutputValidationError, match="failed validation"):
        call(gateway(FakeProvider({"total": "lots"}), FakeBucket(), []))


def _status_error(status: int, headers: dict[str, str] | None = None) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, headers=headers or {}, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 529])
def test_anthropic_retryable_statuses_are_transient(status: int) -> None:
    error = classify(_status_error(status, {"retry-after": "7"}))
    assert isinstance(error, TransientProviderError)
    assert (error.status, error.retry_after) == (status, 7.0)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_anthropic_client_errors_are_permanent(status: int) -> None:
    assert isinstance(classify(_status_error(status)), PermanentProviderError)


def test_anthropic_connection_errors_are_transient() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assert isinstance(
        classify(anthropic.APIConnectionError(request=request)), TransientProviderError
    )


class Recorded:
    """A stand-in Anthropic client that keeps the request and answers with ``message``."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.kwargs: dict[str, Any] = {}
        self.message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            model="claude-x-20261001",
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=11, output_tokens=5),
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.message


class Reading(BaseModel):
    text: str = Field(min_length=1, max_length=40)
    confidence: Decimal = Field(ge=0, le=1)


class Page(BaseModel):
    fields: list[Reading] = Field(min_length=2)
    total: Decimal


def test_anthropic_provider_asks_for_structured_output_not_a_forced_tool() -> None:
    client = Recorded('{"total": 4.10}')
    provider = AnthropicProvider(client)  # type: ignore[arg-type]
    request = JsonRequest("claude-x", "s", "p", Answer.model_json_schema(), 50, effort="low")
    response = provider.complete_json(request)
    assert response == JsonResponse(  # never a float
        {"total": Decimal("4.10")},
        11,
        5,
        finish_reason="end_turn",
        response_model="claude-x-20261001",
    )
    assert "tool_choice" not in client.kwargs
    assert "tools" not in client.kwargs
    config = client.kwargs["output_config"]
    assert config["effort"] == "low"
    assert config["format"]["type"] == "json_schema"
    assert config["format"]["schema"]["additionalProperties"] is False


def test_effort_is_left_to_the_model_unless_set() -> None:
    client = Recorded('{"total": 4}')
    AnthropicProvider(client).complete_json(  # type: ignore[arg-type]
        JsonRequest("claude-x", "s", "p", Answer.model_json_schema(), 50)
    )
    assert "effort" not in client.kwargs["output_config"]


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for item in node for k in _keys(item)}
    return set()


def _objects(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [o for item in node for o in _objects(item)]
    if not isinstance(node, dict):
        return []
    found = [node] if node.get("type") == "object" or "properties" in node else []
    return found + [o for value in node.values() for o in _objects(value)]


def test_strict_schema_drops_what_structured_outputs_reject() -> None:
    schema = strict_schema(Page.model_json_schema())
    assert not _keys(schema) & {"minimum", "maximum", "minLength", "maxLength"}
    assert all(obj["additionalProperties"] is False for obj in _objects(schema))
    assert "(?!" not in json.dumps(schema)  # Decimal's lookahead pattern is gone
    assert "minItems" not in json.dumps(schema)  # only 0 and 1 are allowed
    assert strict_schema({"type": "array", "minItems": 1}) == {"type": "array", "minItems": 1}


def test_strict_schema_refuses_open_ended_objects() -> None:
    open_ended = {"type": "object", "additionalProperties": {"type": "string"}}
    with pytest.raises(PermanentProviderError, match="open-ended"):
        strict_schema(open_ended)


@pytest.mark.parametrize(
    ("text", "stop_reason", "error"),
    [
        ("I can't help with that.", "refusal", "refusal"),
        ('{"total": 4', "max_tokens", "max_tokens"),
        ("not json", "end_turn", "invalid JSON"),
    ],
)
def test_refusals_truncation_and_bad_json_are_invalid_output(
    text: str, stop_reason: str, error: str
) -> None:
    provider = AnthropicProvider(Recorded(text, stop_reason))  # type: ignore[arg-type]
    with pytest.raises(OutputValidationError, match=error) as raised:
        provider.complete_json(JsonRequest("claude-x", "s", "p", Answer.model_json_schema(), 50))
    assert raised.value.finish_reason == (None if stop_reason == "end_turn" else stop_reason)


def test_effort_reaches_the_provider() -> None:
    provider = FakeProvider({"total": 3})
    llm = gateway(provider, FakeBucket(), [])
    llm.structured("fake", "m", system="s", prompt="p", output=Answer, max_tokens=50, effort="max")
    assert provider.requests[0].effort == "max"


def test_a_replayed_answer_reports_the_recorded_latency() -> None:
    class Replayed(FakeProvider):
        def complete_json(self, request: JsonRequest) -> JsonResponse:
            return JsonResponse({"total": 1}, 10, 2, latency_ms=4321)

    result = call(gateway(Replayed(None), FakeBucket(), []))
    assert result.latency_ms == 4321
    assert call(gateway(FakeProvider({"total": 1}), FakeBucket(), [])).latency_ms < 1000


def test_anthropic_provider_classifies_sdk_errors() -> None:
    def overloaded(**_: Any) -> None:
        raise _status_error(529)

    client = SimpleNamespace(messages=SimpleNamespace(create=overloaded))
    provider = AnthropicProvider(client)  # type: ignore[arg-type]
    with pytest.raises(TransientProviderError):
        provider.complete_json(JsonRequest("claude-x", "s", "p", {}, 50))


def test_unlimited_admits_everything() -> None:
    provider = FakeProvider({"total": 3})
    unlimited = LlmGateway({"fake": provider}, Unlimited(), default_limits=LIMITS)
    for _ in range(100):
        unlimited.structured(
            "fake", "m", system="s", prompt="p", output=Answer, max_tokens=1_000_000
        )
    assert len(provider.requests) == 100
