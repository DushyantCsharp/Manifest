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
    assert set(run.retrieval) == {"offline-lexical"}
    summary = hs.summary(run)
    assert summary["accuracy_6"] == summary["recall_at_20"]  # right whenever it was retrieved
    assert summary["accuracy_2"] >= summary["accuracy_4"] >= summary["accuracy_6"]
    assert {o.split for o in run.outcomes} == {"calibration", "test"}


def test_seeding_reproduces_the_committed_recordings(tmp_path: Path) -> None:
    hs.seed(tmp_path)
    for folder in ("hs", "hs-retrieval"):
        committed = {p.name for p in (hs.CASSETTES / folder).glob("*.json")}
        seeded = {p.name for p in (tmp_path / folder).glob("*.json")}
        assert seeded == committed, folder
