"""Idempotency-Key handling for every POST (draft-ietf-httpapi-idempotency-key-header).

A key is claimed atomically together with a fingerprint of the request (method, path, body):

- first use: the handler runs and its 2xx response is stored for the store's TTL;
- same key, same request, finished: the stored response is replayed with
  ``Idempotent-Replayed: true``;
- same key, same request, still running: 409 ``idempotency-key-in-flight``;
- same key, different request: 422 ``idempotency-key-reused``;
- handler raised (any 4xx/5xx, including 429): the claim is released so the client can retry
  with the same key. Only successes are remembered.

Keys are global until there is authentication; then they are scoped per principal.
"""

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Protocol, Self

from fastapi import Header, Request, Response
from pydantic import BaseModel

from manifest_api.errors import IdempotencyKeyInFlightError, IdempotencyKeyReusedError

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"

IdempotencyKey = Annotated[
    str,
    Header(
        alias=IDEMPOTENCY_KEY_HEADER,
        min_length=1,
        max_length=255,
        description=(
            "Client-generated unique key (a UUID is ideal). Retrying with the same key and body "
            "returns the original response instead of repeating the side effect."
        ),
    ),
]


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json(
        cls, status_code: int, model: BaseModel, headers: Mapping[str, str] | None = None
    ) -> Self:
        return cls(status_code, model.model_dump_json().encode(), dict(headers or {}))

    def to_response(self, *, replayed: bool) -> Response:
        headers = dict(self.headers)
        if replayed:
            headers[IDEMPOTENT_REPLAYED_HEADER] = "true"
        return Response(
            self.body, status_code=self.status_code, headers=headers, media_type="application/json"
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    fingerprint: str
    response: StoredResponse | None
    """None while the original request is still running."""


class IdempotencyStore(Protocol):
    async def claim(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        """Atomically claim ``key``. Returns None if claimed, else the record already held."""
        ...

    async def complete(self, key: str, response: StoredResponse) -> None: ...

    async def release(self, key: str) -> None: ...


async def fingerprint(request: Request) -> str:
    digest = hashlib.sha256()
    for part in (request.method.encode(), str(request.url.path).encode(), await request.body()):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


async def run_idempotent(
    store: IdempotencyStore,
    key: str,
    request: Request,
    handler: Callable[[], Awaitable[StoredResponse]],
) -> Response:
    request_fingerprint = await fingerprint(request)
    existing = await store.claim(key, request_fingerprint)
    if existing is not None:
        if existing.fingerprint != request_fingerprint:
            raise IdempotencyKeyReusedError(
                f"{IDEMPOTENCY_KEY_HEADER} {key!r} was used for a different request"
            )
        if existing.response is None:
            raise IdempotencyKeyInFlightError(
                f"the original request with {IDEMPOTENCY_KEY_HEADER} {key!r} has not finished"
            )
        return existing.response.to_response(replayed=True)

    try:
        stored = await handler()
    except BaseException:
        await store.release(key)
        raise
    await store.complete(key, stored)
    return stored.to_response(replayed=False)
