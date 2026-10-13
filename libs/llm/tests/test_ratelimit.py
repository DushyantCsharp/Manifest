"""The Redis token bucket: limits, refusals that debit nothing, and correctness under load.

Every test runs twice: against fakeredis (its Lua runtime, no Docker; part of ``make test``)
and against a real Redis from testcontainers (marked integration).
"""

import math
import secrets
import threading
import time
from collections import Counter
from collections.abc import Iterator

import fakeredis
import pytest
from redis import Redis

from manifest_llm.ratelimit import Limits, TokenBucket


@pytest.fixture(
    params=["fakeredis", pytest.param("redis", marks=pytest.mark.integration)],
)
def redis(request: pytest.FixtureRequest) -> Iterator[Redis]:
    if request.param == "fakeredis":
        yield fakeredis.FakeRedis(server=fakeredis.FakeServer())
        return
    client = Redis.from_url(request.getfixturevalue("redis_url"))
    yield client
    client.close()


@pytest.fixture
def bucket(redis: Redis) -> Iterator[TokenBucket]:
    namespace = f"manifest-test-{secrets.token_hex(4)}"
    yield TokenBucket(redis, namespace=namespace)
    for key in redis.scan_iter(f"{namespace}:*"):
        redis.delete(key)


def test_requests_per_minute(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=3, tokens_per_minute=1_000_000)
    assert [bucket.try_acquire("p", "m", limits, 1) for _ in range(3)] == [0, 0, 0]
    wait = bucket.try_acquire("p", "m", limits, 1)
    assert 19 < wait <= 20  # one request refills every 60/3 s


def test_tokens_per_minute_and_refusals_debit_nothing(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=100, tokens_per_minute=6_000)
    assert bucket.try_acquire("p", "m", limits, 5_000) == 0
    wait = bucket.try_acquire("p", "m", limits, 3_000)  # 2,000 short at 100 tokens/s
    assert 19 < wait <= 20
    assert bucket.try_acquire("p", "m", limits, 900) == 0  # the refusal took nothing


def test_buckets_are_per_provider_and_model(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=1, tokens_per_minute=1_000)
    assert bucket.try_acquire("anthropic", "model-a", limits, 10) == 0
    assert bucket.try_acquire("anthropic", "model-a", limits, 10) > 0
    assert bucket.try_acquire("anthropic", "model-b", limits, 10) == 0
    assert bucket.try_acquire("other", "model-a", limits, 10) == 0


def test_settle_charges_real_usage(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=100, tokens_per_minute=1_000)
    assert bucket.try_acquire("p", "m", limits, 100) == 0
    bucket.settle("p", "m", limits, 850)  # the call really used 950
    assert bucket.try_acquire("p", "m", limits, 100) > 0
    bucket.settle("p", "m", limits, -900)  # ... no: it used 50; credit the difference
    assert bucket.try_acquire("p", "m", limits, 100) == 0
    bucket.settle("p", "m", limits, 0)  # nothing to correct: no round trip


def test_oversized_request_is_capped_to_the_budget(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=10, tokens_per_minute=1_000)
    assert bucket.try_acquire("p", "m", limits, 50_000) == 0


# ---------------------------------------------------------------- concurrency

WORKERS = 8
ATTEMPTS = 25


def hammer(
    bucket: TokenBucket, limits: Limits, tokens: int, models: list[str]
) -> tuple[Counter[str], float]:
    """WORKERS threads each try ATTEMPTS acquires as fast as they can, all released at once."""
    granted: Counter[str] = Counter()
    lock = threading.Lock()
    start = threading.Barrier(WORKERS)

    def worker(index: int) -> None:
        model = models[index % len(models)]
        start.wait()
        for _ in range(ATTEMPTS):
            if bucket.try_acquire("p", model, limits, tokens) == 0:
                with lock:
                    granted[model] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return granted, time.monotonic() - began


def refill(per_minute: int, seconds: float) -> float:
    return per_minute * seconds / 60


def test_concurrent_acquires_never_exceed_the_request_budget(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=40, tokens_per_minute=10_000_000)
    granted, elapsed = hammer(bucket, limits, 1, ["m"])
    assert WORKERS * ATTEMPTS > 40  # far more demand than budget
    assert 40 <= granted["m"] <= 40 + math.floor(refill(40, elapsed))


def test_concurrent_acquires_never_exceed_the_token_budget(
    bucket: TokenBucket, redis: Redis
) -> None:
    """TPM binds first (33 calls of 150 tokens in 5,000); a call TPM refuses takes no request
    slot, so the RPM bucket is down by exactly the calls granted: both debits are atomic.
    """
    limits = Limits(requests_per_minute=60, tokens_per_minute=5_000)
    granted, elapsed = hammer(bucket, limits, 150, ["m"])
    affordable = 5_000 // 150
    assert affordable <= granted["m"] <= (5_000 + refill(5_000, elapsed)) // 150

    rpm_key, tpm_key = bucket.keys("p", "m")
    requests_left = float(redis.hget(rpm_key, "level") or 0)
    tokens_left = float(redis.hget(tpm_key, "level") or 0)
    assert abs(requests_left - (60 - granted["m"])) <= refill(60, elapsed) + 1e-6
    assert abs(tokens_left - (5_000 - 150 * granted["m"])) <= refill(5_000, elapsed) + 1e-6
    assert tokens_left >= 0
    assert requests_left >= 0


def test_concurrent_models_draw_on_their_own_buckets(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=10, tokens_per_minute=10_000_000)
    granted, elapsed = hammer(bucket, limits, 1, ["a", "b"])
    for model in ("a", "b"):
        assert 10 <= granted[model] <= 10 + math.floor(refill(10, elapsed))


def test_a_refused_caller_is_told_long_enough_to_succeed(bucket: TokenBucket) -> None:
    limits = Limits(requests_per_minute=600, tokens_per_minute=10_000_000)  # 1 per 100 ms
    while bucket.try_acquire("p", "m", limits, 1) == 0:
        pass
    wait = bucket.try_acquire("p", "m", limits, 1)
    assert 0 < wait <= 0.1
    time.sleep(wait)
    assert bucket.try_acquire("p", "m", limits, 1) == 0
