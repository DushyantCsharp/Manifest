"""Recorded retrieval: replay a retriever's candidates, keyed by a hash of the query.

Evals replay HS classification without a database or an embedding key: the candidates live
retrieval returned are recorded per query (the index's vectors are too large to commit), and
replays answer from the recording, so the walk's prompts and its cassettes hit. Modes as for
LLM cassettes: ``replay`` (a miss raises), ``record`` and ``new``. A recording names the
retriever that produced it (``offline-lexical`` until one is recorded against a real index).
"""

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from manifest_hs.retrieval import TOP, Candidate, Retriever

Mode = Literal["replay", "record", "new"]
FORMAT = 1


class RetrievalMissError(LookupError):
    """No recording for this query (re-record it with a live retriever)."""


def key(query: str, limit: int) -> str:
    canonical = json.dumps({"query": " ".join(query.split()), "limit": limit}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _candidate(data: dict[str, Any]) -> Candidate:
    return Candidate(data["code"], data["description"], Decimal(data["score"]), data["ranks"])


class RecordedRetriever:
    def __init__(
        self, directory: Path, *, mode: Mode = "replay", inner: Retriever | None = None
    ) -> None:
        if mode != "replay" and inner is None:
            raise ValueError(f"mode {mode!r} needs a retriever to record from")
        self.directory, self.mode, self._inner = directory, mode, inner
        self.name = inner.name if inner is not None else "recorded"
        self.sources: Counter[str] = Counter()  # which retriever produced what was replayed

    def search(self, query: str, limit: int = TOP) -> list[Candidate]:
        path = self.directory / f"{key(query, limit)}.json"
        if self.mode != "record" and path.exists():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            self.sources[recorded["retriever"]] += 1
            return [_candidate(c) for c in recorded["candidates"]]
        if self.mode == "replay" or self._inner is None:
            raise RetrievalMissError(f"no retrieval recording for {query!r} in {self.directory}")
        candidates = self._inner.search(query, limit)
        recording: dict[str, Any] = {
            "format": FORMAT,
            "key": path.stem,
            "retriever": self._inner.name,
            "query": query,
            "limit": limit,
            "candidates": [
                {
                    "code": c.code,
                    "description": c.description,
                    "score": str(c.score),
                    "ranks": c.ranks,
                }
                for c in candidates
            ],
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(recording, indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        return candidates
