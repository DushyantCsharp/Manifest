"""Hybrid retrieval of HS 2022 subheadings: Postgres full-text and pgvector, fused by rank.

Each retriever ranks subheadings for a goods description; reciprocal rank fusion (RRF) merges
the rankings: a code scores ``sum(1 / (RRF_K + rank))`` over the lists it appears in, so a
code near the top of either list rises, and one both lists agree on rises most. RRF needs no
score normalisation across retrievers whose scores aren't comparable (ts_rank vs cosine).

- Lexical: ``hs_codes.path_search``, a tsvector of each code's text weighted with its
  heading's and chapter's (migration 0007), queried with the description's words OR-ed.
- Vector: cosine distance between the description's embedding and each code's path-text
  embedding (``hs_codes.embedding``), only among vectors from the same embedding model.

Without an embedder (no key configured) retrieval is lexical only, and says so (``name``).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import HsCode

RRF_K = 60
DEPTH = 50  # per ranking, before fusion
TOP = 20
_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "other",
        "others",
        "the",
        "to",
        "with",
        "without",
    ]
)
Ranks = dict[str, int | None]


@dataclass(frozen=True)
class Candidate:
    code: str
    description: str
    score: Decimal
    ranks: Ranks = field(default_factory=dict)


class Retriever(Protocol):
    name: str

    def search(self, query: str, limit: int = TOP) -> list[Candidate]: ...


def terms(query: str) -> list[str]:
    """The description's distinct content words, lower-cased, in order."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.lower()):
        if len(word) > 1 and word not in STOPWORDS:
            seen.setdefault(word)
    return list(seen)


def fuse(rankings: Mapping[str, Sequence[str]], k: int = RRF_K) -> list[tuple[str, Decimal, Ranks]]:
    """Reciprocal rank fusion: (code, score, rank in each ranking), best first."""
    scores: dict[str, Decimal] = {}
    ranks: dict[str, Ranks] = {}
    for name, ranking in rankings.items():
        for rank, code in enumerate(ranking, start=1):
            scores[code] = scores.get(code, Decimal(0)) + Decimal(1) / (k + rank)
            ranks.setdefault(code, dict.fromkeys(rankings))[name] = rank
    ordered = sorted(scores, key=lambda code: (-scores[code], code))
    return [(code, scores[code].quantize(Decimal("0.000001")), ranks[code]) for code in ordered]


def lexical_query(words: Sequence[str], depth: int = DEPTH) -> Select[str]:
    """Subheadings matching any word, best ``ts_rank_cd`` first."""
    query = func.to_tsquery("english", " | ".join(words))
    return (
        select(HsCode.code)
        .where(HsCode.level == 6, HsCode.path_search.op("@@")(query))
        .order_by(func.ts_rank_cd(HsCode.path_search, query).desc(), HsCode.code)
        .limit(depth)
    )


def vector_query(vector: Sequence[float], model: str, depth: int = DEPTH) -> Select[str]:
    """Subheadings nearest by cosine distance, among vectors from ``model``."""
    distance = HsCode.embedding.cosine_distance(list(vector))
    return (
        select(HsCode.code)
        .where(HsCode.level == 6, HsCode.embedding_model == model)
        .order_by(distance, HsCode.code)
        .limit(depth)
    )


class PostgresRetriever:
    """Hybrid retrieval against ``hs_codes``; lexical only without an embedder."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        embed: Callable[[str], list[float]] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._embed = embed if embedding_model else None
        self._model = embedding_model
        self.name = "hybrid" if self._embed else "lexical"

    def search(self, query: str, limit: int = TOP) -> list[Candidate]:
        words = terms(query)
        rankings: dict[str, list[str]] = {"lexical": [], "vector": []}
        with self._sessions() as session:
            if words:
                rankings["lexical"] = list(session.scalars(lexical_query(words)))
            if self._embed is not None and self._model is not None:
                # Filtered HNSW scans keep going until enough rows pass the filter (pgvector 0.8).
                session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                vector = self._embed(query)
                rankings["vector"] = list(session.scalars(vector_query(vector, self._model)))
            fused = fuse(rankings)[:limit]
            codes = [code for code, _, _ in fused]
            described = select(HsCode.code, HsCode.description).where(HsCode.code.in_(codes))
            texts = dict(session.execute(described).tuples().all())
        return [Candidate(code, texts[code], score, ranks) for code, score, ranks in fused]
