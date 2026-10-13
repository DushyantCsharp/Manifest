"""Embeddings: the offline hashing embedder, the gateway's embed path, Voyage over HTTP, prices."""

import json
import math
from decimal import Decimal
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from manifest_llm.embeddings import EmbedRequest, EmbedResponse, HashingEmbedder
from manifest_llm.errors import (
    OutputValidationError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from manifest_llm.gateway import LlmGateway, estimate_embedding_tokens
from manifest_llm.pricing import cost
from manifest_llm.ratelimit import Limits
from manifest_llm.voyage_provider import VoyageProvider

LIMITS = Limits(requests_per_minute=60, tokens_per_minute=10_000)


class Bucket:
    def __init__(self, wait: float = 0.0) -> None:
        self.wait = wait
        self.acquired: list[int] = []
        self.settled: list[int] = []

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        self.acquired.append(tokens)
        return self.wait

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None:
        self.settled.append(delta)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------- hashing embedder


@given(st.text(max_size=60))
def test_hashing_vectors_are_deterministic_unit_vectors(text: str) -> None:
    vector = HashingEmbedder.vector(text, 64)
    assert vector == HashingEmbedder.vector(text, 64)
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0) or norm == 0.0  # no words: the zero vector


def test_hashing_vectors_measure_word_overlap() -> None:
    shirt = HashingEmbedder.vector("men's cotton t-shirt, knitted", 1024)
    similar = HashingEmbedder.vector("T-shirts of cotton, knitted or crocheted", 1024)
    unrelated = HashingEmbedder.vector("live horses for breeding", 1024)
    assert cosine(shirt, similar) > cosine(shirt, unrelated)


# ---------------------------------------------------------------- gateway


def test_embed_admits_settles_and_keeps_order() -> None:
    bucket = Bucket()
    llm = LlmGateway({}, bucket, default_limits=LIMITS, embedders={"hashing": HashingEmbedder()})
    texts = ["cotton t-shirts", "ball bearings", "frozen shrimp"]
    result = llm.embed("hashing", "hashing-v1", texts, input_type="document", dimensions=32)
    assert result.vectors == [HashingEmbedder.vector(t, 32) for t in texts]
    request = EmbedRequest("hashing-v1", tuple(texts), "document", 32)
    assert bucket.acquired == [estimate_embedding_tokens(request)]
    assert bucket.settled == [result.tokens - estimate_embedding_tokens(request)]


def test_embed_rate_limited_never_calls_the_provider() -> None:
    class Never:
        name = "never"

        def embed(self, request: EmbedRequest) -> EmbedResponse:
            raise AssertionError("called while rate limited")

    llm = LlmGateway({}, Bucket(wait=30), default_limits=LIMITS, embedders={"never": Never()})
    with pytest.raises(RateLimitedError):
        llm.embed("never", "m", ["x"], input_type="query", dimensions=8)


def test_embed_rejects_the_wrong_shape() -> None:
    class Short:
        name = "short"

        def embed(self, request: EmbedRequest) -> EmbedResponse:
            return EmbedResponse([[0.0] * (request.dimensions - 1) for _ in request.texts], 3)

    llm = LlmGateway({}, Bucket(), default_limits=LIMITS, embedders={"short": Short()})
    with pytest.raises(OutputValidationError, match="dimensions"):
        llm.embed("short", "m", ["a", "b"], input_type="query", dimensions=8)


# ---------------------------------------------------------------- Voyage


def voyage(handler: Any) -> VoyageProvider:
    client = httpx.Client(base_url="https://voyage.test/v1", transport=httpx.MockTransport(handler))
    return VoyageProvider(client)


REQUEST = EmbedRequest("voyage-3.5", ("first", "second"), "document", 4)


def test_voyage_request_and_response() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        rows = [  # out of order on purpose: the provider sorts by index
            {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
            {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
        ]
        return httpx.Response(200, json={"data": rows, "usage": {"total_tokens": 7}})

    response = voyage(handler).embed(REQUEST)
    assert response == EmbedResponse([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], 7)
    assert sent == [
        {
            "input": ["first", "second"],
            "model": "voyage-3.5",
            "input_type": "document",
            "output_dimension": 4,
        }
    ]


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_voyage_retryable_statuses_are_transient(status: int) -> None:
    provider = voyage(lambda _: httpx.Response(status, headers={"retry-after": "3"}, text="busy"))
    with pytest.raises(TransientProviderError) as caught:
        provider.embed(REQUEST)
    assert (caught.value.status, caught.value.retry_after) == (status, 3.0)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_voyage_client_errors_are_permanent(status: int) -> None:
    with pytest.raises(PermanentProviderError):
        voyage(lambda _: httpx.Response(status, text="no")).embed(REQUEST)


def test_voyage_connection_failures_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TransientProviderError, match="unreachable"):
        voyage(handler).embed(REQUEST)


def test_voyage_malformed_bodies_are_invalid_output() -> None:
    with pytest.raises(OutputValidationError):
        voyage(lambda _: httpx.Response(200, json={"data": "nope"})).embed(REQUEST)


def test_voyage_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(PermanentProviderError, match="VOYAGE_API_KEY"):
        VoyageProvider()
    request = EmbedRequest("voyage-3.5", ("x",) * 1_001, "query", 4)
    with pytest.raises(PermanentProviderError, match="at most"):
        voyage(lambda _: httpx.Response(200)).embed(request)


# ---------------------------------------------------------------- prices


def test_cost_in_usd_per_call() -> None:
    assert cost("claude-sonnet-5-5", 1_000_000, 100_000) == Decimal(3)
    assert cost("anthropic/claude-sonnet-5-5", 4_000, 2_000) == Decimal("0.028")
    assert cost("voyage-3.5", 2_000_000) == Decimal("0.12")
    assert cost("claude-unknown", 10, 10) is None
