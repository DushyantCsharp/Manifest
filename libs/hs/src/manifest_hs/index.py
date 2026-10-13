"""Build the HS vector index: ``python -m manifest_hs.index`` (``make hs-index``).

Embeds every subheading's path text (its text with its heading's and chapter's) and stores the
vector with the model that produced it. Resumable and idempotent: only rows whose
``embedding_model`` isn't the requested model are embedded, a batch at a time, each batch
committed, so an interrupted build continues where it stopped and switching models re-embeds
everything. Calls go through the LLM gateway like every model call; a rate-limited or failed
batch is retried with backoff.

    python -m manifest_hs.index                      # voyage-3.5 (VOYAGE_API_KEY)
    python -m manifest_hs.index --provider hashing   # offline vectors, for tests and demos
"""

import argparse
import time
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import EMBEDDING_DIMENSIONS, HsCode
from manifest_db.session import sync_session_factory
from manifest_hs.nomenclature import Nomenclature, hs2022
from manifest_llm.embeddings import EmbeddingProvider, HashingEmbedder
from manifest_llm.errors import RateLimitedError, TransientProviderError
from manifest_llm.gateway import LlmGateway, Unlimited
from manifest_llm.ratelimit import Limits
from manifest_llm.voyage_provider import VoyageProvider

DEFAULT_MODEL = "voyage-3.5"
BATCH = 128
ATTEMPTS = 5


def pending(session: Session, model: str, limit: int) -> list[str]:
    """Subheadings without a vector from ``model``."""
    query = (
        select(HsCode.code)
        .where(HsCode.level == 6, HsCode.embedding_model.is_distinct_from(model))
        .order_by(HsCode.code)
        .limit(limit)
    )
    return list(session.scalars(query))


def _embed_with_retries(
    gateway: LlmGateway,
    provider: str,
    model: str,
    texts: list[str],
    sleep: Callable[[float], None],
) -> list[list[float]]:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            embedded = gateway.embed(
                provider, model, texts, input_type="document", dimensions=EMBEDDING_DIMENSIONS
            )
            return embedded.vectors
        except RateLimitedError as exc:
            sleep(exc.retry_after)
        except TransientProviderError as exc:
            if attempt == ATTEMPTS:
                raise
            sleep(exc.retry_after or 2**attempt)
    raise TransientProviderError(f"still rate limited after {ATTEMPTS} attempts")


def build(
    sessions: sessionmaker[Session],
    gateway: LlmGateway,
    provider: str,
    model: str,
    *,
    nomenclature: Nomenclature | None = None,
    batch: int = BATCH,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int], None] = lambda _: None,
) -> int:
    """Embed every pending subheading; returns how many were embedded."""
    tree = nomenclature or hs2022()
    done = 0
    while True:
        with sessions() as session:
            codes = pending(session, model, batch)
        if not codes:
            return done
        texts = [tree.path(code) for code in codes]
        vectors = _embed_with_retries(gateway, provider, model, texts, sleep)
        with sessions.begin() as session:
            for code, vector in zip(codes, vectors, strict=True):
                session.execute(
                    update(HsCode)
                    .where(HsCode.code == code)
                    .values(embedding=vector, embedding_model=model)
                )
        done += len(codes)
        progress(done)


def gateway_for(embedder: EmbeddingProvider) -> LlmGateway:
    """A gateway for the build: the provider's own limits pace it (429s back off)."""
    limits = Limits(requests_per_minute=300, tokens_per_minute=1_000_000)
    return LlmGateway({}, Unlimited(), default_limits=limits, embedders={embedder.name: embedder})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m manifest_hs.index", description=__doc__)
    parser.add_argument("--provider", choices=["voyage", "hashing"], default="voyage")
    parser.add_argument("--model", default=None, help=f"default: {DEFAULT_MODEL} / hashing-v1")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--database-url", default=None, help="default: MANIFEST_DB_URL")
    args = parser.parse_args(argv)
    hashing = args.provider == "hashing"
    embedder: EmbeddingProvider = HashingEmbedder() if hashing else VoyageProvider()
    model = args.model or (HashingEmbedder.model if hashing else DEFAULT_MODEL)
    total = build(
        sync_session_factory(args.database_url),
        gateway_for(embedder),
        embedder.name,
        model,
        batch=args.batch,
        progress=lambda n: print(f"embedded {n}", flush=True),
    )
    print(f"{total} subheadings embedded with {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
