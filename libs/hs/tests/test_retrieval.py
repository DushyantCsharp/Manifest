"""The nomenclature, rank fusion, the retrieval SQL, offline BM25 and recorded retrieval."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from manifest_hs.nomenclature import SOURCE, hs2022, read
from manifest_hs.offline import Bm25Retriever, stem
from manifest_hs.recorded import RecordedRetriever, RetrievalMissError
from manifest_hs.retrieval import Candidate, fuse, lexical_query, terms, vector_query


def test_the_nomenclature_is_the_pinned_hs2022_tree() -> None:
    tree = hs2022()
    levels = [entry.level for entry in tree.entries.values()]
    assert (levels.count(2), levels.count(4), levels.count(6)) == (97, 1229, 5613)
    assert tree["610910"].parent == "6109"
    assert [e.code for e in tree.ancestors("610910")] == ["61", "6109", "610910"]
    path = tree.path("610910")
    assert path.startswith("Section XI. Chapter 61: ")
    assert "Heading 6109: " in path
    assert path.endswith("of cotton, knitted or crocheted")
    assert "610910" in tree.children["6109"]


def test_a_tampered_nomenclature_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "hs.csv"
    copy.write_bytes(SOURCE.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="not the pinned"):
        read(copy)


def test_terms_are_distinct_content_words() -> None:
    assert terms("Men's T-shirts, of cotton; knitted & T-shirts") == [
        "men",
        "shirts",
        "cotton",
        "knitted",
    ]
    assert terms("of the") == []


def test_reciprocal_rank_fusion() -> None:
    fused = fuse({"lexical": ["a", "b", "c"], "vector": ["c", "a"]}, k=60)
    assert [code for code, _, _ in fused] == ["a", "c", "b"]
    a = {code: score for code, score, _ in fused}["a"]
    assert a == (Decimal(1) / 61 + Decimal(1) / 62).quantize(Decimal("0.000001"))
    assert fused[2][2] == {"lexical": 2, "vector": None}


def _sql(statement: object) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled)


def test_the_retrieval_sql() -> None:
    lexical = _sql(lexical_query(["cotton", "shirts"]))
    assert "to_tsquery" in lexical
    assert "hs_codes.path_search @@" in lexical
    assert "ts_rank_cd(hs_codes.path_search" in lexical
    assert "hs_codes.level =" in lexical
    vector = _sql(vector_query([0.1, 0.2], "voyage-3.5"))
    assert "<=>" in vector  # cosine distance
    assert "hs_codes.embedding_model =" in vector


def test_bm25_finds_what_the_words_say() -> None:
    retriever = Bm25Retriever()
    found = [c.code for c in retriever.search("Men's crew-neck T-shirt, knitted cotton jersey")]
    assert "610910" in found  # among the 20 the walk chooses from (lexical rank is no judge)
    assert len(found) == 20
    bearings = [c.code for c in retriever.search("Deep groove ball bearings")]
    assert "848210" in bearings[:3]
    assert retriever.search("Men's crew-neck T-shirt, knitted cotton jersey")[0].code == found[0]
    assert retriever.search("zzzz") == []
    assert (stem("shirts"), stem("batteries"), stem("glass")) == ("shirt", "battery", "glass")


class Fixed:
    name = "fixed"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, limit: int = 20) -> list[Candidate]:
        self.calls += 1
        return [Candidate("610910", "T-shirts; of cotton", Decimal("0.03"), {"lexical": 1})]


def test_recorded_retrieval_replays_without_the_retriever(tmp_path: Path) -> None:
    inner = Fixed()
    recorded = RecordedRetriever(tmp_path, mode="record", inner=inner).search("cotton  t-shirts")
    replayer = RecordedRetriever(tmp_path)
    assert replayer.search("cotton t-shirts") == recorded  # spacing doesn't matter
    assert replayer.sources == {"fixed": 1}
    with pytest.raises(RetrievalMissError):
        replayer.search("frozen shrimp")
    fresh = RecordedRetriever(tmp_path, mode="new", inner=inner)
    fresh.search("cotton t-shirts")
    fresh.search("frozen shrimp")
    assert inner.calls == 2  # recorded once, then only what was missing
    with pytest.raises(ValueError, match="needs a retriever"):
        RecordedRetriever(tmp_path, mode="record")
