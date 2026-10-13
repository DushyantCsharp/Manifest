"""The HS index and hybrid retrieval against real Postgres + pgvector (hashing embeddings)."""

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import EMBEDDING_DIMENSIONS, HsCode
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_hs import index
from manifest_hs.retrieval import PostgresRetriever
from manifest_llm.embeddings import HashingEmbedder

pytestmark = pytest.mark.integration
MODEL = HashingEmbedder.model


@pytest.fixture(scope="module")
def sessions(postgres_url: str) -> Iterator[sessionmaker[Session]]:
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        yield sync_session_factory(database)


def embedded(sessions: sessionmaker[Session], model: str) -> int:
    with sessions() as session:
        query = select(func.count()).select_from(HsCode).where(HsCode.embedding_model == model)
        return session.scalar(query) or 0


def test_migration_fills_the_path_search(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        missing = session.scalar(
            select(func.count()).select_from(HsCode).where(HsCode.path_search.is_(None))
        )
        assert missing == 0
        weighted = session.scalar(select(HsCode.path_search).where(HsCode.code == "610910"))
        assert "'apparel':23C" in str(weighted)  # the chapter's word, carried down at weight C
        assert "'cotton':9A" in str(weighted)  # the subheading's own, at weight A


def test_the_build_is_resumable_and_retrieval_is_hybrid(sessions: sessionmaker[Session]) -> None:
    embedder = HashingEmbedder()
    gateway = index.gateway_for(embedder)
    first = index.build(sessions, gateway, embedder.name, MODEL, batch=500)
    assert first == embedded(sessions, MODEL) == 5613
    assert index.build(sessions, gateway, embedder.name, MODEL) == 0  # nothing left to do

    def embed(text: str) -> list[float]:
        return HashingEmbedder.vector(text, EMBEDDING_DIMENSIONS)

    hybrid = PostgresRetriever(sessions, embed, MODEL)
    found = hybrid.search("Men's T-shirts of cotton, knitted")
    assert hybrid.name == "hybrid"
    assert len(found) == 20
    assert "610910" in [c.code for c in found]  # hashing vectors know words, not meaning
    assert any(c.ranks["vector"] for c in found)
    assert any(c.ranks["lexical"] for c in found)

    lexical = PostgresRetriever(sessions)
    assert lexical.name == "lexical"
    assert all(c.ranks["vector"] is None for c in lexical.search("cotton t-shirts"))
    other = PostgresRetriever(sessions, embed, "another-model")  # no vectors from it
    assert all(c.ranks["vector"] is None for c in other.search("cotton t-shirts"))
