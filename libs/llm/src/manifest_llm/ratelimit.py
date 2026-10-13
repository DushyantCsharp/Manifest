"""Shared RPM and TPM token buckets in Redis, keyed by provider and model.

Both buckets are checked and debited in one Lua script against the Redis server's clock, so
every worker draws on the same budget, worker clock skew doesn't matter, and a request can never
take its tokens without also taking its request slot. A refused acquire returns how long to
wait instead of debiting anything; callers sleep briefly or re-queue, they never spin.
"""

from dataclasses import dataclass

from redis import Redis

BUCKET_TTL_MS = 120_000  # an idle bucket is full after a minute anyway

# KEYS: rpm bucket, tpm bucket. ARGV: rpm limit, tpm limit, tokens wanted, ttl ms.
# Returns 0 when granted (both buckets debited), else milliseconds to wait (nothing debited).
_ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local function level(key, capacity)
  local state = redis.call('HMGET', key, 'level', 'at')
  local value, at = tonumber(state[1]), tonumber(state[2])
  if value == nil then return capacity end
  return math.min(capacity, value + (now - at) * capacity / 60000)
end
local rpm, tpm = tonumber(ARGV[1]), tonumber(ARGV[2])
local tokens, ttl = tonumber(ARGV[3]), tonumber(ARGV[4])
local requests, budget = level(KEYS[1], rpm), level(KEYS[2], tpm)
local wait = 0
if requests < 1 then wait = math.max(wait, (1 - requests) * 60000 / rpm) end
if budget < tokens then wait = math.max(wait, (tokens - budget) * 60000 / tpm) end
if wait == 0 then
  requests = requests - 1
  budget = budget - tokens
end
redis.call('HSET', KEYS[1], 'level', tostring(requests), 'at', now)
redis.call('HSET', KEYS[2], 'level', tostring(budget), 'at', now)
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('PEXPIRE', KEYS[2], ttl)
return math.ceil(wait)
"""

# KEYS: tpm bucket. ARGV: tpm limit, tokens to debit (negative credits), ttl ms.
_SETTLE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local capacity, delta, ttl = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'level', 'at')
local value, at = tonumber(state[1]), tonumber(state[2])
if value == nil then value, at = capacity, now end
value = math.min(capacity, value + (now - at) * capacity / 60000) - delta
redis.call('HSET', KEYS[1], 'level', tostring(math.min(capacity, value)), 'at', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return 0
"""


@dataclass(frozen=True)
class Limits:
    requests_per_minute: int
    tokens_per_minute: int


class TokenBucket:
    def __init__(self, redis: Redis, *, namespace: str = "manifest") -> None:
        self._namespace = namespace
        self._acquire = redis.register_script(_ACQUIRE)
        self._settle = redis.register_script(_SETTLE)

    def keys(self, provider: str, model: str) -> tuple[str, str]:
        # Hash tag: both buckets live in the same cluster slot, as one script requires.
        base = f"{self._namespace}:ratelimit:{{{provider}:{model}}}"
        return f"{base}:rpm", f"{base}:tpm"

    def try_acquire(self, provider: str, model: str, limits: Limits, tokens: int) -> float:
        """Take one request and ``tokens`` tokens. Returns 0.0, or seconds to wait (no debit).

        A request larger than the whole per-minute budget is capped at it, so it can still run.
        """
        wanted = min(tokens, limits.tokens_per_minute)
        args = [limits.requests_per_minute, limits.tokens_per_minute, wanted, BUCKET_TTL_MS]
        wait_ms = int(self._acquire(keys=list(self.keys(provider, model)), args=args))
        return wait_ms / 1000

    def settle(self, provider: str, model: str, limits: Limits, delta: int) -> None:
        """Correct the token bucket for real usage (positive delta: used more than estimated)."""
        if delta:
            _, tpm = self.keys(provider, model)
            self._settle(keys=[tpm], args=[limits.tokens_per_minute, delta, BUCKET_TTL_MS])
