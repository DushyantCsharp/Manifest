"""Idempotency keys in Redis (``manifest_api.idempotency.IdempotencyStore``).

A claim is ``SET NX`` of the request's fingerprint with a short TTL, so a request that dies
mid-flight frees its key after ``in_flight_ttl`` instead of answering 409 for a day. Completing
stores the response next to the fingerprint for ``ttl``. Only successes are stored.
"""

import base64
import json
from typing import Any

from redis.asyncio import Redis

from manifest_api.idempotency import IdempotencyRecord, StoredResponse


class RedisIdempotencyStore:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl: int,
        in_flight_ttl: int = 300,
        namespace: str = "manifest",
    ) -> None:
        self._redis = redis
        self._ttl, self._in_flight_ttl = ttl, in_flight_ttl
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self._namespace}:idempotency:{key}"

    async def claim(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        stored = json.dumps({"fingerprint": fingerprint})
        for _ in range(3):  # the holder's claim can expire between our SET and GET
            if await self._redis.set(self._key(key), stored, nx=True, ex=self._in_flight_ttl):
                return None
            raw = await self._redis.get(self._key(key))
            if raw is not None:
                return _record(json.loads(raw))
        return None

    async def complete(self, key: str, response: StoredResponse) -> None:
        raw = await self._redis.get(self._key(key))
        fingerprint = json.loads(raw)["fingerprint"] if raw is not None else ""
        record = {
            "fingerprint": fingerprint,
            "response": {
                "status_code": response.status_code,
                "body": base64.b64encode(response.body).decode(),
                "headers": dict(response.headers),
            },
        }
        await self._redis.set(self._key(key), json.dumps(record), ex=self._ttl)

    async def release(self, key: str) -> None:
        await self._redis.delete(self._key(key))


def _record(data: dict[str, Any]) -> IdempotencyRecord:
    response = data.get("response")
    if response is None:
        return IdempotencyRecord(data["fingerprint"], None)
    stored = StoredResponse(
        response["status_code"], base64.b64decode(response["body"]), response["headers"]
    )
    return IdempotencyRecord(data["fingerprint"], stored)
