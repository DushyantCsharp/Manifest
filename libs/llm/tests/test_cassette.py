"""Cassettes: request-hash keys, record/replay/new modes, misses that explain themselves."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from manifest_llm.cassette import CassetteMissError, CassetteProvider, request_key
from manifest_llm.errors import PermanentProviderError
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway
from manifest_llm.ratelimit import Limits, TokenBucket

SCHEMA = {"type": "object", "properties": {"total": {"type": "number"}}}


def request(prompt: str = "PAGE 1\nTotal 12.50", **changes: Any) -> JsonRequest:
    fields: dict[str, Any] = {
        "model": "claude-test",
        "system": "Extract.",
        "prompt": prompt,
        "schema": SCHEMA,
        "max_tokens": 256,
    }
    return JsonRequest(**(fields | changes))


class Live:
    name = "anthropic"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        self.calls += 1
        return JsonResponse({"total": 12.5, "echo": request.prompt[:6]}, 40, 8)


texts = st.text(max_size=40)


@given(texts, texts)
def test_keys_ignore_spacing_but_not_content(prompt: str, other: str) -> None:
    spaced = "  " + prompt.replace(" ", " \n\t ") + "\n"
    assert request_key("p", request(prompt)) == request_key("p", request(spaced))
    if prompt.split() != other.split():
        assert request_key("p", request(prompt)) != request_key("p", request(other))


@pytest.mark.parametrize(
    "change",
    [
        {"model": "claude-other"},
        {"system": "Extract carefully."},
        {"schema": {"type": "object"}},
        {"max_tokens": 512},
    ],
)
def test_keys_cover_everything_that_changes_the_answer(change: dict[str, Any]) -> None:
    assert request_key("p", request(**change)) != request_key("p", request())
    assert request_key("other", request()) != request_key("p", request())


def test_record_then_replay_without_calling_out(tmp_path: Path) -> None:
    live = Live()
    recorder = CassetteProvider(tmp_path, mode="record", inner=live)
    recorded = recorder.complete_json(request())
    assert (live.calls, recorder.counts["recorded"]) == (1, 1)

    [path] = tmp_path.glob("*.json")
    recording = json.loads(path.read_text())
    assert path.stem == recording["key"] == request_key("anthropic", request())
    assert recording["recorded_with"] == "anthropic"
    assert recording["request"]["prompt"] == request().prompt

    player = CassetteProvider(tmp_path, name="anthropic")
    replayed = player.complete_json(request(prompt="PAGE 1 Total 12.50"))  # spacing differs
    assert replayed.data == {"total": Decimal("12.5"), "echo": "PAGE 1"}  # never a float
    assert (replayed.input_tokens, replayed.output_tokens) == (40, 8)
    assert recorded.data["total"] == 12.5
    assert player.counts["replayed"] == 1
    assert live.calls == 1


def test_a_miss_explains_itself(tmp_path: Path) -> None:
    CassetteProvider(tmp_path, mode="record", inner=Live()).complete_json(request())
    player = CassetteProvider(tmp_path, name="anthropic")
    with pytest.raises(CassetteMissError) as caught:
        player.complete_json(request("PAGE 1\nTotal 13.50"))
    message = str(caught.value)
    assert "make test-live" in message
    assert "-Total 12.50" in message
    assert "+Total 13.50" in message
    assert isinstance(caught.value, PermanentProviderError)  # never retried


def test_a_miss_in_an_empty_cassette(tmp_path: Path) -> None:
    with pytest.raises(CassetteMissError, match="no recording"):
        CassetteProvider(tmp_path / "none", name="anthropic").complete_json(request())


def test_new_mode_records_only_what_is_missing(tmp_path: Path) -> None:
    live = Live()
    cassette = CassetteProvider(tmp_path, mode="new", inner=live)
    cassette.complete_json(request())
    cassette.complete_json(request())
    cassette.complete_json(request("PAGE 2"))
    assert live.calls == 2
    assert cassette.counts == {"recorded": 2, "replayed": 1}


def test_record_mode_overwrites(tmp_path: Path) -> None:
    first, second = Live(), Live()
    CassetteProvider(tmp_path, mode="record", inner=first).complete_json(request())
    CassetteProvider(tmp_path, mode="record", inner=second).complete_json(request())
    assert (first.calls, second.calls) == (1, 1)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_stand_ins_record_under_the_provider_they_stand_in_for(tmp_path: Path) -> None:
    oracle = Live()
    oracle.name = "oracle"  # type: ignore[misc]
    CassetteProvider(
        tmp_path, mode="record", inner=oracle, name="anthropic", recorded_with="ground truth"
    ).complete_json(request())
    [path] = tmp_path.glob("*.json")
    recording = json.loads(path.read_text())
    assert recording["provider"] == "anthropic"
    assert recording["recorded_with"] == "ground truth"
    assert CassetteProvider(tmp_path, name="anthropic").complete_json(request()).data


def test_construction_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a provider"):
        CassetteProvider(tmp_path, mode="record", name="anthropic")
    with pytest.raises(ValueError, match="name the provider"):
        CassetteProvider(tmp_path)


class Total(BaseModel):
    total: Decimal


def test_replay_goes_through_the_gateway(tmp_path: Path) -> None:
    """Replayed calls still take rate-limit budget and are validated like live ones."""
    sent = JsonRequest("claude-test", "Extract.", "PAGE 1", Total.model_json_schema(), 64)
    CassetteProvider(tmp_path, mode="record", inner=Live()).complete_json(sent)
    bucket = TokenBucket(fakeredis.FakeRedis(server=fakeredis.FakeServer()), namespace="t")
    limits = Limits(requests_per_minute=1, tokens_per_minute=100_000)
    gateway = LlmGateway(
        {"anthropic": CassetteProvider(tmp_path, name="anthropic")},
        bucket,
        default_limits=limits,
        max_inline_wait=0,
    )
    result = gateway.structured(
        "anthropic", "claude-test", system="Extract.", prompt="PAGE 1", output=Total, max_tokens=64
    )
    assert result.output.total == Decimal("12.5")
    assert bucket.try_acquire("anthropic", "claude-test", limits, 1) > 0  # budget was spent


def test_effort_changes_the_key_only_when_set() -> None:
    unset = request_key("p", request())
    assert request_key("p", request(effort=None)) == unset  # recordings made before effort
    assert request_key("p", request(effort="low")) != unset
    assert request_key("p", request(effort="low")) != request_key("p", request(effort="high"))


def test_recordings_keep_the_live_latency_and_replays_report_it(tmp_path: Path) -> None:
    class Slow(Live):
        def complete_json(self, request: JsonRequest) -> JsonResponse:
            return JsonResponse({"total": 1}, 40, 8, latency_ms=2_345)

    CassetteProvider(tmp_path, mode="record", inner=Slow()).complete_json(request(effort="low"))
    [path] = tmp_path.glob("*.json")
    recording = json.loads(path.read_text())
    assert recording["response"]["latency_ms"] == 2_345
    assert recording["request"]["effort"] == "low"
    replayed = CassetteProvider(tmp_path, name="anthropic").complete_json(request(effort="low"))
    assert replayed.latency_ms == 2_345


def test_live_latency_is_measured_when_the_provider_has_none(tmp_path: Path) -> None:
    CassetteProvider(tmp_path, mode="record", inner=Live()).complete_json(request())
    [path] = tmp_path.glob("*.json")
    assert isinstance(json.loads(path.read_text())["response"]["latency_ms"], int)


def test_replays_count_who_answered(tmp_path: Path) -> None:
    oracle = Live()
    oracle.name = "oracle"  # type: ignore[misc]
    CassetteProvider(
        tmp_path, mode="record", inner=oracle, name="anthropic", recorded_with="ground truth"
    ).complete_json(request())
    player = CassetteProvider(tmp_path, name="anthropic")
    player.complete_json(request())
    assert player.sources == {"ground truth": 1}
