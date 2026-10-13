"""Recording eval answers from real models: retries, resumability, parallelism.

Offline tooling only (the pipeline retries through Celery). A recording run:

- retries transient provider errors (429, overload, timeouts) with backoff, honouring the
  provider's ``retry-after``;
- is resumable: it first deletes only the stand-in recordings (the oracle's answers, offline
  retrieval), then records in ``new`` mode, so an interrupted run keeps every real answer it
  already paid for;
- records several packs or descriptions at once (``WORKERS``), well inside rate limits.
"""

import json
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from manifest_evals.oracle import NAME as ORACLE
from manifest_llm.errors import TransientProviderError
from manifest_llm.gateway import JsonRequest, JsonResponse, Provider

ATTEMPTS = 6
WORKERS = 4
MAX_WAIT_S = 60.0
OFFLINE_RETRIEVAL = "offline-lexical"


class RetryingProvider:
    """``inner``, retried on transient errors with exponential backoff."""

    def __init__(self, inner: Provider, sleep: Callable[[float], None] = time.sleep) -> None:
        self._inner, self._sleep = inner, sleep
        self.name = inner.name

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return self._inner.complete_json(request)
            except TransientProviderError as exc:
                if attempt == ATTEMPTS:
                    raise
                self._sleep(min(exc.retry_after or 2.0**attempt, MAX_WAIT_S))
        raise RuntimeError("unreachable")  # pragma: no cover


def forget_stand_ins(directory: Path, field: str = "recorded_with") -> int:
    """Delete recordings a stand-in made (the oracle, or offline retrieval); keep real ones."""
    removed = 0
    for path in directory.glob("*.json") if directory.is_dir() else ():
        source = str(json.loads(path.read_text(encoding="utf-8")).get(field) or "")
        if source.startswith(ORACLE) or source == OFFLINE_RETRIEVAL:
            path.unlink()
            removed += 1
    return removed


def in_parallel[T, R](
    items: Iterable[T], work: Callable[[T], R], workers: int = WORKERS
) -> list[R]:
    """``work`` over ``items``, a few at a time, in order; the first failure propagates."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, items))
