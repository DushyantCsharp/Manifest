"""The worker's HS classifier: the walk behind the port, wired from settings alone."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from manifest_hs.retrieval import Candidate
from manifest_hs.walk import Walk
from manifest_worker.app import WorkerSettings
from manifest_worker.hs import LazyNotes, NoSuggestionError, WalkClassifier
from manifest_worker.ports import LineToClassify
from manifest_worker.runtime import build_hs_classifier

CANDIDATE = Candidate("610910", "T-shirts; of cotton", Decimal("0.03"), {"lexical": 1})


class FakeWalker:
    def __init__(self, code: str | None) -> None:
        self.code = code
        self.asked: list[tuple[str, str | None]] = []

    def classify(self, description: str, origin: str | None = None) -> Walk:
        self.asked.append((description, origin))
        raw = Decimal("0.72") if self.code else Decimal(0)
        step = {"level": "chapter", "reason": "apparel", "options": ["61"], "choice": "61"}
        return Walk(self.code, raw, [CANDIDATE], [step], "hybrid", "anthropic/test")


def line(description: str | None = "Men's cotton T-shirt") -> LineToClassify:
    return LineToClassify(uuid4(), description, "BD")


def test_a_walk_becomes_a_suggestion() -> None:
    walker = FakeWalker("610910")
    result = WalkClassifier(walker).classify(line())  # type: ignore[arg-type]
    assert (result.code, result.confidence, result.model) == (
        "610910",
        Decimal("0.72"),
        "anthropic/test",
    )
    assert result.candidates == [
        {
            "code": "610910",
            "description": "T-shirts; of cotton",
            "score": "0.03",
            "ranks": {"lexical": 1},
        }
    ]
    assert result.walk is not None
    assert result.walk["candidates"] == ["610910"]
    assert result.rationale == "Chapter: apparel"
    assert walker.asked == [("Men's cotton T-shirt", "BD")]  # description and origin only


@pytest.mark.parametrize(
    ("walker", "described"), [(FakeWalker(None), "tee"), (FakeWalker("610910"), None)]
)
def test_no_fitting_code_is_no_suggestion(walker: FakeWalker, described: str | None) -> None:
    with pytest.raises(NoSuggestionError):
        WalkClassifier(walker).classify(line(described))  # type: ignore[arg-type]


def test_hs_is_wired_from_settings_without_connecting() -> None:
    sessions = sessionmaker()  # unbound: building must not query anything
    none = WorkerSettings.model_validate({"ANTHROPIC_API_KEY": None})
    assert build_hs_classifier(none, sessions) is None
    keyed = WorkerSettings.model_validate(
        {"ANTHROPIC_API_KEY": SecretStr("sk-test"), "VOYAGE_API_KEY": SecretStr("pa-test")}
    )
    assert isinstance(build_hs_classifier(keyed, sessions), WalkClassifier)


def test_notes_load_on_first_use() -> None:
    loads: list[int] = []

    class Counting(LazyNotes):
        def _loaded(self) -> dict:  # type: ignore[type-arg]
            loads.append(1)
            return {}

    notes = Counting(sessionmaker())
    assert loads == []
    assert dict(notes) == {}
    assert loads
