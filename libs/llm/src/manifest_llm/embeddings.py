"""Embedding models behind the gateway: the request/response shapes, and an offline embedder.

``HashingEmbedder`` maps text to a deterministic vector by feature hashing (words and word
pairs into signed buckets, L2-normalised). It needs no model or network, so tests and offline
tools can build and query a pgvector index; it measures word overlap, not meaning, so it is
never what production retrieves with.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, Protocol

InputType = Literal["query", "document"]
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class EmbedRequest:
    model: str
    texts: tuple[str, ...]
    input_type: InputType
    dimensions: int


@dataclass(frozen=True)
class EmbedResponse:
    vectors: list[list[float]]
    tokens: int


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, request: EmbedRequest) -> EmbedResponse:
        """Raise ``TransientProviderError``/``PermanentProviderError``; never retry internally."""
        ...


def _bucket(feature: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    number = int.from_bytes(digest, "big")
    return number % dimensions, 1.0 if number >> 63 else -1.0


class HashingEmbedder:
    """Deterministic bag-of-words vectors: for tests and offline tooling only."""

    name = "hashing"
    model = "hashing-v1"

    def embed(self, request: EmbedRequest) -> EmbedResponse:
        vectors = [self.vector(text, request.dimensions) for text in request.texts]
        tokens = sum(len(_WORD.findall(text)) for text in request.texts)
        return EmbedResponse(vectors, tokens)

    @staticmethod
    def vector(text: str, dimensions: int) -> list[float]:
        words = [word.lower() for word in _WORD.findall(text)]
        features = words + [f"{a} {b}" for a, b in pairwise(words)]
        vector = [0.0] * dimensions
        for feature in features:
            index, sign = _bucket(feature, dimensions)
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector
