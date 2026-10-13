"""HS evals: the stand-in model, and recordings that stay in step with the walk."""

from pathlib import Path

import pytest

from manifest_evals import hs
from manifest_hs.walk import NONE, answer_model
from manifest_llm.gateway import JsonRequest

LABEL = hs.labels()[0]


def ask(level: str, options: list[str], goods: str = LABEL.description) -> dict[str, object]:
    request = JsonRequest(
        "m",
        "s",
        f"Goods: {goods}\nCountry of origin: CN\n\nChoose the {level} that covers these goods.",
        answer_model(options).model_json_schema(),
        100,
    )
    data: dict[str, object] = hs.HsOracle(hs.labels()).complete_json(request).data
    return data


def test_the_stand_in_picks_the_label_when_offered_and_guesses_when_not() -> None:
    right = LABEL.hs6[:2]
    offered = ask("chapter", ["99", right])
    assert (offered["choice"], offered["confidence"], offered["cited"]) == (right, "0.95", [right])
    missed = ask("chapter", ["99", "98"])
    assert (missed["choice"], missed["confidence"]) == ("99", hs.GUESS)
    assert NONE not in (offered["choice"], missed["choice"])
    with pytest.raises(ValueError, match="not an HS walk request"):
        hs.HsOracle(hs.labels()).complete_json(JsonRequest("m", "s", "hello", {}, 1))


def test_the_committed_recordings_replay() -> None:
    run = hs.evaluate()
    assert len(run.outcomes) == 120
    assert set(run.retrieval) == {"hybrid"}
    assert set(run.sources) == {"anthropic"}, "a stand-in answer was committed"
    summary = hs.summary(run)
    assert summary["accuracy_6"] <= summary["recall_at_20"]  # it only chooses retrieved codes
    assert summary["accuracy_2"] >= summary["accuracy_4"] >= summary["accuracy_6"]
    assert {o.split for o in run.outcomes} == {"calibration", "test"}


def test_the_stand_ins_are_right_exactly_when_the_label_was_retrieved(tmp_path: Path) -> None:
    hs.seed(tmp_path)
    run = hs.evaluate(hs.cassettes(root=tmp_path), hs.retrievals(root=tmp_path))
    assert set(run.retrieval) == {"offline-lexical"}
    summary = hs.summary(run)
    assert summary["accuracy_6"] == summary["recall_at_20"]
    # Retrieval is keyed by the query alone, so stand-ins cover the recorded descriptions.
    committed = {p.name for p in (hs.CASSETTES / "hs-retrieval").glob("*.json")}
    assert {p.name for p in (tmp_path / "hs-retrieval").glob("*.json")} == committed
