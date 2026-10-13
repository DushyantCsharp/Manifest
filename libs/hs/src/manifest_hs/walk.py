"""HS classification: retrieve candidates, then walk the tree chapter → heading → subheading.

1. Retrieval (``retrieval``) returns the 20 subheadings closest to the goods description.
2. The model picks a chapter among the chapters those candidates belong to, seeing each
   chapter's title, its legal notes (``hs_notes``, when vendored) and the candidates under it.
   Deciding the chapter first, at chapter level, keeps a lexically similar subheading in the
   wrong chapter from winning (cotton T-shirts are apparel, not cotton).
3. Then a heading among the candidates' headings in that chapter, then a subheading among the
   candidates under that heading.

Each step is one structured call whose answer must be one of the options shown, or ``none``
when no option can cover the goods (the walk stops: no suggestion, which routes to review).
The model cites the codes (and notes) it relied on; a citation of anything that wasn't shown
sets that step's confidence to 0, as unverifiable evidence does in extraction. A step with
a single option isn't asked. The walk's raw confidence is the product of its steps' (each is
the probability of being right given the steps before), calibrated later by the ``hs``
calibrator. The description and origin are the only inputs: never a code printed on the
document, which would turn classification into copying.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from math import prod
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, create_model

from manifest_hs.nomenclature import Nomenclature, hs2022
from manifest_hs.retrieval import TOP, Candidate, Retriever
from manifest_llm.gateway import Effort, LlmGateway

PROMPT_VERSION = "2026-10-13"
MODEL = "claude-sonnet-5-5"
EFFORT: Effort = "medium"
MAX_TOKENS = 8_000
NONE = "none"
Level = Literal["chapter", "heading", "subheading"]
LEVELS: tuple[Level, ...] = ("chapter", "heading", "subheading")
DIGITS: dict[Level, int] = {"chapter": 2, "heading": 4, "subheading": 6}

SYSTEM = """You classify goods in the Harmonized System 2022 (HS), one level at a time.

You are given a goods description and a list of options at one level of the nomenclature \\
(chapter, heading or subheading), each with its legal text, any notes, and the retrieved \\
subheadings it contains. Choose the option whose text and notes cover these goods, applying \\
the General Interpretative Rules: the terms of the headings and the section and chapter \\
notes decide. If no option can cover the goods, answer "none".

- choice: the code of one option, or "none".
- cited: the codes (and note ids) of the texts you relied on; cite only what is shown.
- confidence: your probability, from 0 to 1, that the choice is the correct one at this \\
level. It is checked against labelled goods, so be calibrated, not modest or optimistic.
- reason: one sentence explaining the choice."""


@dataclass(frozen=True)
class Note:
    """A legal note the classifier may cite (``hs_notes``)."""

    scope: str  # section, chapter or heading
    ref: str  # "XI", "61", "6109"
    number: int
    text: str

    @property
    def id(self) -> str:
        return f"note:{self.ref}:{self.number}"


@dataclass(frozen=True)
class Option:
    code: str
    text: str
    contents: tuple[Candidate, ...]  # the retrieved subheadings under it
    notes: tuple[Note, ...] = ()


@dataclass
class Walk:
    """One classification: the code (None when the model answered "none" or retrieval found
    nothing), its raw confidence, and every step for the record."""

    code: str | None
    raw: Decimal
    candidates: list[Candidate]
    steps: list[dict[str, Any]] = field(default_factory=list)
    retriever: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def rationale(self) -> str:
        return " ".join(f"{s['level'].title()}: {s['reason']}" for s in self.steps if s["reason"])

    def record(self) -> dict[str, Any]:
        return {
            "retriever": self.retriever,
            "candidates": [c.code for c in self.candidates],
            "steps": self.steps,
            "prompt_version": PROMPT_VERSION,
        }


def answer_model(options: Sequence[str]) -> type[BaseModel]:
    """The structured answer to one step: a choice among exactly these options."""
    choice: Any = Literal[(*options, NONE)]  # the options are only known per request
    number = Annotated[Decimal, WithJsonSchema({"type": "number"})]
    return create_model(
        "HsStep",
        __config__=ConfigDict(extra="forbid"),
        choice=(choice, Field(description='An option code, or "none".')),
        cited=(list[str], Field(description="Codes and note ids relied on, from those shown.")),
        confidence=(number, Field(ge=0, le=1, description="Probability the choice is right.")),
        reason=(str, Field(description="One sentence.")),
    )


def options_at(
    level: Level,
    candidates: Sequence[Candidate],
    within: str,
    nomenclature: Nomenclature,
    notes: Mapping[str, Sequence[Note]],
) -> list[Option]:
    """The level's codes among the candidates under ``within``, best-ranked first."""
    digits = DIGITS[level]
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.code.startswith(within):
            grouped.setdefault(candidate.code[:digits], []).append(candidate)
    return [
        Option(code, nomenclature[code].description, tuple(contents), tuple(notes.get(code, ())))
        for code, contents in grouped.items()
    ]


def prompt(
    description: str, origin: str | None, level: Level, options: Sequence[Option], path: str
) -> str:
    lines = [f"Goods: {description}", f"Country of origin: {origin or 'not stated'}", ""]
    if path:
        lines += [f"Chosen so far: {path}", ""]
    lines += [f"Choose the {level} that covers these goods. Options:"]
    for option in options:
        lines.append(f"- {option.code}: {option.text}")
        lines += [f"    {note.id}: {note.text}" for note in option.notes]
        if level != "subheading":
            lines += [f"    contains {c.code}: {c.description}" for c in option.contents]
    lines += ["", 'Answer "none" if no option can cover these goods.']
    return "\n".join(lines)


def shown(options: Sequence[Option], path_codes: Sequence[str]) -> set[str]:
    """Every id the model may cite at this step."""
    ids = set(path_codes)
    for option in options:
        ids.add(option.code)
        ids.update(c.code for c in option.contents)
        ids.update(c.code[:4] for c in option.contents)
        ids.update(note.id for note in option.notes)
    return ids


class HsWalker:
    def __init__(
        self,
        gateway: LlmGateway,
        provider: str,
        retriever: Retriever,
        *,
        model: str = MODEL,
        nomenclature: Nomenclature | None = None,
        notes: Mapping[str, Sequence[Note]] | None = None,
        effort: Effort | None = EFFORT,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._gateway, self._provider, self._retriever = gateway, provider, retriever
        self._model, self._effort, self._max_tokens = model, effort, max_tokens
        self._nomenclature = nomenclature or hs2022()
        self._notes = notes if notes is not None else {}  # may be lazy: never truth-test it

    def classify(self, description: str, origin: str | None = None) -> Walk:
        query = f"{description} origin {origin}" if origin else description
        candidates = self._retriever.search(query, TOP)
        walk = Walk(None, Decimal(0), candidates, retriever=self._retriever.name)
        walk.model = f"{self._provider}/{self._model}"
        within, path_codes, confidences = "", list[str](), list[Decimal]()
        for level in LEVELS:
            options = options_at(level, candidates, within, self._nomenclature, self._notes)
            if not options:
                return walk
            step = self._step(walk, level, description, origin, options, path_codes)
            walk.steps.append(step)
            if step["choice"] == NONE:
                return walk
            confidences.append(Decimal(step["confidence"]))
            within = step["choice"]
            path_codes.append(within)
        walk.code = within
        walk.raw = prod(confidences, start=Decimal(1)).quantize(Decimal("0.0001"))
        return walk

    def _step(
        self,
        walk: Walk,
        level: Level,
        description: str,
        origin: str | None,
        options: Sequence[Option],
        path_codes: Sequence[str],
    ) -> dict[str, Any]:
        codes = [option.code for option in options]
        record: dict[str, Any] = {"level": level, "options": codes, "skipped": len(codes) == 1}
        if len(codes) == 1:  # nothing to choose
            answer = {"choice": codes[0], "cited": [], "confidence": "1", "reason": ""}
            return record | answer | {"valid_citations": True}
        path = " > ".join(f"{c} {self._nomenclature[c].description}" for c in path_codes)
        result = self._gateway.structured(
            self._provider,
            self._model,
            system=SYSTEM,
            prompt=prompt(description, origin, level, options, path),
            output=answer_model(codes),
            max_tokens=self._max_tokens,
            effort=self._effort,
        )
        answer = result.output.model_dump()
        walk.input_tokens += result.input_tokens
        walk.output_tokens += result.output_tokens
        walk.latency_ms += result.latency_ms
        cited = [str(c) for c in answer["cited"]]
        valid = set(cited) <= shown(options, path_codes)
        confidence = Decimal(str(answer["confidence"])) if valid else Decimal(0)
        return record | {
            "choice": answer["choice"],
            "cited": cited,
            "valid_citations": valid,
            "confidence": str(confidence),
            "reason": str(answer["reason"]),
        }
