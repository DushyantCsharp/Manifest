"""Voyage AI embeddings as an ``EmbeddingProvider`` (plain HTTP; no SDK).

Errors are sorted like the Anthropic provider's: 429, 408, 409 and 5xx, timeouts and connection
failures are transient; other client errors (bad key, unknown model, input too long) are not.
Never retries itself: the caller (Celery, or the resumable index build) does.
"""

import os

import httpx

from manifest_llm.embeddings import EmbedRequest, EmbedResponse
from manifest_llm.errors import (
    OutputValidationError,
    PermanentProviderError,
    TransientProviderError,
)

API_URL = "https://api.voyageai.com/v1"
MAX_BATCH = 1_000  # texts per request (the API's limit)
TRANSIENT_STATUSES = frozenset({408, 409, 429})


class VoyageProvider:
    name = "voyage"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 60,
    ) -> None:
        """``api_key`` defaults to the VOYAGE_API_KEY environment variable."""
        key = api_key or os.environ.get("VOYAGE_API_KEY")
        if client is None and not key:
            raise PermanentProviderError("VOYAGE_API_KEY is not set")
        self._client = client or httpx.Client(
            base_url=API_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout
        )

    def embed(self, request: EmbedRequest) -> EmbedResponse:
        if len(request.texts) > MAX_BATCH:
            raise PermanentProviderError(f"at most {MAX_BATCH} texts per request")
        body = {
            "input": list(request.texts),
            "model": request.model,
            "input_type": request.input_type,
            "output_dimension": request.dimensions,
        }
        try:
            response = self._client.post("/embeddings", json=body)
        except httpx.TransportError as exc:  # timeouts, connection failures
            raise TransientProviderError(f"voyage unreachable: {exc}") from exc
        if response.status_code in TRANSIENT_STATUSES or response.status_code >= 500:
            header = response.headers.get("retry-after", "")
            retry_after = float(header) if header.replace(".", "", 1).isdigit() else None
            raise TransientProviderError(
                f"voyage {response.status_code}: {response.text[:200]}",
                status=response.status_code,
                retry_after=retry_after,
            )
        if response.is_error:
            raise PermanentProviderError(
                f"voyage {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
            rows = sorted(payload["data"], key=lambda row: row["index"])
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
            tokens = int(payload["usage"]["total_tokens"])
        except (ValueError, KeyError, TypeError) as exc:
            raise OutputValidationError(f"voyage answered an unexpected body: {exc}") from exc
        return EmbedResponse(vectors, tokens)
