import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Allows ``rate`` acquisitions per second on average, in bursts of at most ``burst``.

    In-process: it paces one backfill runner, which the advisory lock keeps unique. LLM calls
    are paced separately by the shared Redis bucket in ``manifest_llm``.
    """

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await self._sleep((1 - self._tokens) / self._rate)
