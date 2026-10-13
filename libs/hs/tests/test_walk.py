"""The walk: options per level, skipped steps, "none", citation checks, chained confidence."""

import json
import re
from decimal import Decimal
from typing import Any

from manifest_hs.retrieval import Candidate
from manifest_hs.walk import NONE, HsWalker, Note, answer_model, prompt, shown
from manifest_llm.gateway import JsonRequest, JsonResponse, LlmGateway, Unlimited
from manifest_llm.ratelimit import Limits

CANDIDATES = [
    Candidate("610910", "T-shirts...; of cotton, knitted", Decimal("0.03"), {}),
    Candidate("520522", "Cotton yarn; single, combed", Decimal("0.02"), {}),
    Candidate("610990", "T-shirts...; of other textile materials", Decimal("0.02"), {}),
    Candidate("620520", "Shirts; men's, of cotton", Decimal("0.01"), {}),
]


class Fixed:
    name = "fixed"

    def search(self, query: str, limit: int = 20) -> list[Candidate]:
        return CANDIDATES


class Scripted:
    """Answers each level with a scripted choice, citations and confidence."""

    name = "scripted"

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.requests: list[JsonRequest] = []

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        self.requests.append(request)
        level = re.search(r"Choose the (\w+) that", request.prompt)
        assert level is not None
        data = {"cited": [], "reason": "because"} | self.answers[level.group(1)]
        return JsonResponse(data, 100, 20, latency_ms=50)


def walker(model: Scripted, notes: dict[str, list[Note]] | None = None) -> HsWalker:
    limits = Limits(requests_per_minute=60, tokens_per_minute=100_000)
    gateway = LlmGateway({"scripted": model}, Unlimited(), default_limits=limits)
    return HsWalker(gateway, "scripted", Fixed(), notes=notes)


CHAPTER = {"choice": "61", "cited": ["61", "610910"], "confidence": "0.9"}
SUBHEADING = {"choice": "610910", "cited": ["610910"], "confidence": "0.8"}


def test_chapter_then_heading_then_subheading() -> None:
    model = Scripted({"chapter": CHAPTER, "subheading": SUBHEADING})
    walk = walker(model).classify("Men's cotton T-shirt", "BD")
    assert walk.code == "610910"
    assert walk.raw == Decimal("0.72")  # 0.9, then 1 (one heading: not asked), then 0.8
    assert [s["level"] for s in walk.steps] == ["chapter", "heading", "subheading"]
    assert walk.steps[1]["skipped"] is True  # 6109 is the only heading of 61 retrieved
    assert len(model.requests) == 2
    chapter_options = model.requests[0].schema["properties"]["choice"]["enum"]
    assert chapter_options == ["61", "52", "62", NONE]  # best-ranked chapter first
    assert "Country of origin: BD" in model.requests[0].prompt
    assert (walk.input_tokens, walk.output_tokens, walk.latency_ms) == (200, 40, 100)
    assert walk.record()["candidates"] == [c.code for c in CANDIDATES]
    assert "Chapter: because" in walk.rationale


def test_citing_what_wasnt_shown_zeroes_the_step() -> None:
    made_up = {**CHAPTER, "cited": ["61", "Explanatory Note to 6109"]}
    walk = walker(Scripted({"chapter": made_up, "subheading": SUBHEADING})).classify("tee")
    assert walk.steps[0]["valid_citations"] is False
    assert walk.steps[0]["confidence"] == "0"
    assert (walk.code, walk.raw) == ("610910", 0)  # still suggested, for a reviewer


def test_none_of_these_stops_the_walk() -> None:
    walk = walker(Scripted({"chapter": {"choice": NONE, "confidence": "0.7"}})).classify("tee")
    assert (walk.code, walk.raw) == (None, 0)
    assert len(walk.steps) == 1


def test_nothing_retrieved_means_no_suggestion() -> None:
    class Empty:
        name = "empty"

        def search(self, query: str, limit: int = 20) -> list[Candidate]:
            return []

    limits = Limits(requests_per_minute=60, tokens_per_minute=100_000)
    gateway = LlmGateway({}, Unlimited(), default_limits=limits)
    walk = HsWalker(gateway, "scripted", Empty()).classify("mystery")
    assert (walk.code, walk.steps) == (None, [])


def test_notes_are_shown_and_citable() -> None:
    note = Note("chapter", "61", 1, "This Chapter applies only to made up knitted articles.")
    model = Scripted({"chapter": {**CHAPTER, "cited": ["note:61:1"]}, "subheading": SUBHEADING})
    walk = walker(model, {"61": [note]}).classify("tee")
    assert "note:61:1: This Chapter applies only" in model.requests[0].prompt
    assert walk.steps[0]["valid_citations"] is True


def test_the_answer_schema_offers_exactly_the_options() -> None:
    schema = answer_model(["61", "62"]).model_json_schema()
    assert schema["properties"]["choice"]["enum"] == ["61", "62", NONE]
    assert schema["properties"]["confidence"] == {
        "description": "Probability the choice is right.",
        "title": "Confidence",
        "type": "number",
    }
    assert "anyOf" not in json.dumps(schema)


def test_prompts_and_citable_ids() -> None:
    walk = walker(Scripted({"chapter": CHAPTER, "subheading": SUBHEADING})).classify("tee")
    assert walk.steps[0]["options"] == ["61", "52", "62"]
    text = prompt("tee", None, "heading", [], "61 Apparel")
    assert "Country of origin: not stated" in text
    assert "Chosen so far: 61 Apparel" in text
    assert shown([], ["61"]) == {"61"}
