"""The one way to call a model: shared rate limits, classified errors, validated output.

``LlmGateway.structured`` admits the call against the provider/model token bucket (waiting
briefly in-process, or raising ``RateLimitedError`` so the caller re-queues), calls the
provider, settles the bucket with real token usage and validates the answer against a Pydantic
model. ``LlmGateway.embed`` does the same for embedding models. Provider-level retries are the
caller's job (Celery), so providers never retry.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from manifest_llm.embeddings import EmbeddingProvider, EmbedRequest, InputType
from manifest_llm.errors import OutputValidationError, RateLimitedError
from manifest_llm.ratelimit import Limits

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class JsonRequest:
    model: str
    system: str
    prompt: str
    schema: dict[str, Any]  # JSON Schema the answer must follow
    max_tokens: int  # thinking counts towards it on models that think
    effort: Effort | None = None  # None: the model's default


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    input_tokens: int
    output_tokens: int
    latency_ms: int | None = None  # set by replays: how long the recorded call took


class RateLimiter(Protocol):
    """Shared admission control; ``TokenBucket`` in production."""

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        """0 when admitted, else seconds to wait (nothing taken)."""
        ...

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None: ...


class Unlimited:
    """Admits everything: offline tools that replay or seed recordings, never production."""

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        return 0.0

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None:
        return None


class Provider(Protocol):
    name: str

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        """Raise ``TransientProviderError``/``PermanentProviderError``; never retry internally."""
        ...


@dataclass(frozen=True)
class Structured[T: BaseModel]:
    output: T
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    provider: str
    model: str
    tokens: int
    latency_ms: int


def estimate_tokens(request: JsonRequest) -> int:
    """Upper-bound guess for admission: ~4 characters per token in, plus the output budget."""
    characters = len(request.system) + len(request.prompt) + len(json.dumps(request.schema))
    return characters // 4 + request.max_tokens


def estimate_embedding_tokens(request: EmbedRequest) -> int:
    """Generous guess for admission (~3 characters per token): settled with real usage."""
    return sum(len(text) for text in request.texts) // 3 + len(request.texts)


class LlmGateway:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        bucket: RateLimiter,
        *,
        default_limits: Limits,
        limits: Mapping[tuple[str, str], Limits] | None = None,
        max_inline_wait: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        embedders: Mapping[str, EmbeddingProvider] | None = None,
    ) -> None:
        self._providers = providers
        self._embedders = dict(embedders or {})
        self._bucket = bucket
        self._default_limits = default_limits
        self._limits = dict(limits or {})
        self._max_inline_wait = max_inline_wait
        self._sleep = sleep

    def limits_for(self, provider: str, model: str) -> Limits:
        return self._limits.get((provider, model), self._default_limits)

    def admit(self, provider: str, model: str, tokens: int) -> None:
        """Take budget for one call, sleeping only for short waits; else ``RateLimitedError``."""
        limits = self.limits_for(provider, model)
        wait = self._bucket.try_acquire(provider, model, limits, tokens)
        if wait and wait <= self._max_inline_wait:
            self._sleep(wait)
            wait = self._bucket.try_acquire(provider, model, limits, tokens)
        if wait:
            raise RateLimitedError(wait)

    def structured[T: BaseModel](
        self,
        provider: str,
        model: str,
        *,
        system: str,
        prompt: str,
        output: type[T],
        max_tokens: int,
        effort: Effort | None = None,
    ) -> Structured[T]:
        schema = output.model_json_schema()
        request = JsonRequest(model, system, prompt, schema, max_tokens, effort)
        estimate = estimate_tokens(request)
        self.admit(provider, model, estimate)
        started = time.monotonic()
        response = self._providers[provider].complete_json(request)
        latency_ms = _latency(started, response.latency_ms)
        used = response.input_tokens + response.output_tokens
        self._bucket.settle(provider, model, self.limits_for(provider, model), used - estimate)
        try:
            parsed = output.model_validate(response.data)
        except ValidationError as exc:
            message = f"{provider}/{model} answer failed validation: {exc}"
            raise OutputValidationError(message) from exc
        return Structured(
            parsed, provider, model, response.input_tokens, response.output_tokens, latency_ms
        )

    def embed(
        self,
        provider: str,
        model: str,
        texts: Sequence[str],
        *,
        input_type: InputType,
        dimensions: int,
    ) -> Embedded:
        """One vector per text, each ``dimensions`` long, in order."""
        request = EmbedRequest(model, tuple(texts), input_type, dimensions)
        estimate = estimate_embedding_tokens(request)
        self.admit(provider, model, estimate)
        started = time.monotonic()
        response = self._embedders[provider].embed(request)
        latency_ms = _latency(started, None)
        limits = self.limits_for(provider, model)
        self._bucket.settle(provider, model, limits, response.tokens - estimate)
        if len(response.vectors) != len(texts) or any(
            len(vector) != dimensions for vector in response.vectors
        ):
            shape = f"{len(response.vectors)} vectors for {len(texts)} texts"
            raise OutputValidationError(f"{provider}/{model} returned {shape} or wrong dimensions")
        return Embedded(response.vectors, provider, model, response.tokens, latency_ms)


def _latency(started: float, recorded: int | None) -> int:
    """A replayed answer reports the recorded call's latency, not the replay's."""
    return recorded if recorded is not None else int((time.monotonic() - started) * 1000)
