"""Isotonic calibration: raw confidence scores to probabilities that match observed accuracy.

``fit_isotonic`` pools adjacent violators (PAV) over (raw score, was it right) samples, so the
fitted map never ranks a higher score as less likely to be right. Each pooled block is
smoothed to (right + 1) / (n + 2), so a block of 40 right answers maps to 41/42, not to a
certainty no finite sample supports, and the map interpolates linearly between blocks.

A ``Calibrator`` is a fitted map plus what it was fitted on and for: the scoring recipe
(``scoring.SCORE_VERSION``), extraction schema, prompt and model, and whether the answers came
from a real model or from the ground-truth oracle that seeds cassettes offline. Calibrators
are versioned JSON committed under ``manifest_domain/calibrators`` (written by
``make calibrate``); each extraction records which one produced its confidences. ``trusted``
says whether routing may auto-approve on it: never on an oracle fit.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from importlib import resources
from importlib.resources.abc import Traversable
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from manifest_domain.confidence import CEILING, FLOOR, stored

Provenance = Literal["model", "oracle"]
Knot = tuple[Decimal, Decimal]
_PLACES = Decimal("0.000001")


class FitSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    split: str
    items: int = Field(ge=0, description="Packs (or HS descriptions) the samples came from.")
    samples: int = Field(ge=0)
    positives: int = Field(ge=0)


class Calibrator(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: int = Field(ge=1)
    method: Literal["isotonic"] = "isotonic"
    provenance: Provenance
    score_version: int
    schema_version: int
    prompt_version: str
    model: str
    knots: tuple[Knot, ...] = Field(min_length=1)
    threshold: Decimal | None = Field(default=None, ge=0, le=1)
    """Auto-approve when the least confident value reaches this (calibrated scale)."""
    target_precision: Decimal | None = Field(default=None, ge=0, le=1)
    fitted_on: FitSummary
    metrics: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _monotone(self) -> "Calibrator":
        for (x0, y0), (x1, y1) in pairwise(self.knots):
            if not (x0 < x1 and y0 <= y1):
                raise ValueError("knots must rise in raw score and never fall in probability")
        return self

    @property
    def label(self) -> str:
        return f"{self.name}-v{self.version}"

    @property
    def trusted(self) -> bool:
        """Fitted on a real model's answers, so its probabilities mean something."""
        return self.provenance == "model"

    def apply(self, raw: Decimal) -> Decimal:
        """The calibrated probability for a raw score (piecewise linear between knots)."""
        knots = self.knots
        probability = knots[-1][1]
        if raw <= knots[0][0]:
            probability = knots[0][1]
        elif raw < knots[-1][0]:
            for (x0, y0), (x1, y1) in pairwise(knots):
                if x0 <= raw <= x1:
                    probability = y0 + (raw - x0) * (y1 - y0) / (x1 - x0)
                    break
        return stored(min(max(probability, FLOOR), CEILING))


@dataclass
class _Block:
    n: int
    right: int
    total: Decimal  # sum of the block's raw scores

    @property
    def smoothed(self) -> Decimal:
        return Decimal(self.right + 1) / Decimal(self.n + 2)

    def absorb(self, other: "_Block") -> None:
        self.n, self.right, self.total = (
            self.n + other.n,
            self.right + other.right,
            self.total + other.total,
        )


def fit_isotonic(samples: Iterable[tuple[Decimal, bool]]) -> tuple[Knot, ...]:
    """Knots of the monotone map from raw score to smoothed observed accuracy."""
    groups: dict[Decimal, _Block] = {}
    for score, right in samples:
        block = groups.setdefault(score, _Block(0, 0, Decimal(0)))
        block.absorb(_Block(1, int(right), score))
    if not groups:
        raise ValueError("no samples to fit")
    blocks: list[_Block] = []
    for score in sorted(groups):
        blocks.append(groups[score])
        while len(blocks) > 1 and blocks[-2].smoothed > blocks[-1].smoothed:
            last = blocks.pop()
            blocks[-1].absorb(last)
    knots: list[Knot] = []
    for block in blocks:
        x = (block.total / block.n).quantize(_PLACES, ROUND_HALF_EVEN)
        y = block.smoothed.quantize(_PLACES, ROUND_HALF_EVEN)
        if knots and x <= knots[-1][0]:  # rounding merged two scores: keep the higher
            knots[-1] = (knots[-1][0], max(knots[-1][1], y))
        else:
            knots.append((x, y))
    return tuple(knots)


def dumps(calibrator: Calibrator) -> str:
    """Canonical JSON (Decimals as strings), so a refit compares byte for byte."""
    data = calibrator.model_dump(mode="json")
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def loads(text: str) -> Calibrator:
    return Calibrator.model_validate_json(text)


def _artifacts() -> Traversable:
    return resources.files("manifest_domain") / "calibrators"


def filename(name: str, version: int) -> str:
    return f"{name}-v{version}.json"


def load(name: str, version: int) -> Calibrator:
    """A committed calibrator (``FileNotFoundError`` if there is no such version)."""
    return loads((_artifacts() / filename(name, version)).read_text(encoding="utf-8"))


def versions(name: str) -> list[int]:
    """Committed versions of calibrator ``name``, oldest first."""
    prefix = f"{name}-v"
    found = []
    directory = _artifacts()
    if not directory.is_dir():
        return []
    for entry in directory.iterdir():
        stem = entry.name.removesuffix(".json")
        if entry.name.endswith(".json") and stem.startswith(prefix):
            found.append(int(stem.removeprefix(prefix)))
    return sorted(found)


def artifact_path(name: str, version: int) -> Path:
    """Where ``make calibrate`` writes a calibrator (the package's own source tree)."""
    return Path(str(_artifacts() / filename(name, version)))
