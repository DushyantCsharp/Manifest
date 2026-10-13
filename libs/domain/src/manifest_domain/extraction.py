"""What extraction produces for one page, and the line-item fields each schema version defines.

A ``PageFragment`` is the contract between the page extractor and reconciliation: every value is
already normalised (decimals as strings, ISO 8601 dates, ISO 4217 currencies, ISO 3166-1
alpha-2 countries, weights in kg) and carries a confidence. Reconciliation merges the fragments
of a document's pages into one extraction.

From schema v3 every value also carries its provenance: the page it was read from, the verbatim
evidence span the model quoted, and the signals its confidence is built from (``Signals``).
"""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CURRENT_SCHEMA_VERSION = 3
PROVENANCE_SINCE = 3  # schema versions from this one on record page, evidence and signals

# Line-item fields each released schema version defines (frozen once released).
LINE_FIELDS: dict[int, dict[str, str]] = {
    1: {
        "description": "text",
        "quantity": "decimal",
        "unit": "text",
        "unit_price": "decimal",
        "amount": "decimal",
        "currency": "currency",
        "net_weight_kg": "decimal",
        "gross_weight_kg": "decimal",
    },
}
LINE_FIELDS[2] = {**LINE_FIELDS[1], "country_of_origin": "country"}
LINE_FIELDS[3] = dict(LINE_FIELDS[2])  # v3 adds provenance, not fields

Evidence = Literal["verified", "failed"]
"""Whether the quoted span is on the page and contains the value (``manifest_domain.evidence``)."""
Normalised = Literal["exact", "ambiguous", "failed"]
"""``ambiguous``: only the page's conventions decided it (``1,234``, ``04/07/2026``)."""
Agreement = Literal["agree", "disagree", "single"]
"""Against the same field on the shipment's other documents; ``single``: nothing to compare."""


class Signals(BaseModel):
    """What a value's confidence is built from (``manifest_domain.scoring``)."""

    model_config = ConfigDict(frozen=True)

    self_report: Decimal = Field(ge=0, le=1)
    evidence: Evidence
    normaliser: Normalised
    agreement: Agreement | None = None  # set by cross-document reconciliation


class ExtractedValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str | None = None
    confidence: Decimal = Field(ge=0, le=1)
    page: int | None = Field(default=None, ge=1)
    evidence: str | None = None
    signals: Signals | None = None


def _problem(kind: str, value: str) -> str | None:
    """Why ``value`` isn't a valid normalised value of ``kind``, or None if it is."""
    if kind == "decimal":
        try:
            return None if Decimal(value).is_finite() else "is not finite"
        except InvalidOperation:
            return "is not a decimal"
    code_length = {"currency": 3, "country": 2}.get(kind)
    if code_length and not (len(value) == code_length and value.isascii() and value.isupper()):
        return f"is not an ISO {'4217' if kind == 'currency' else '3166-1'} code"
    return None


def _unproven(extracted: ExtractedValue) -> str | None:
    """Why a v3 value lacks the provenance it must carry, or None."""
    if extracted.page is None or extracted.signals is None:
        return "has no page or signals"
    if extracted.value is not None and not extracted.evidence:
        return "has no evidence"
    return None


class PageFragment(BaseModel):
    """Fields and line items read from one page. Line items are in reading order."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(ge=1)
    fields: dict[str, ExtractedValue] = Field(default_factory=dict)
    line_items: list[dict[str, ExtractedValue]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _line_items_match_schema(self) -> "PageFragment":
        allowed = LINE_FIELDS.get(self.schema_version)
        if allowed is None:
            raise ValueError(f"unknown schema version {self.schema_version}")
        for index, line in enumerate(self.line_items, start=1):
            if unknown := sorted(set(line) - set(allowed)):
                raise ValueError(f"line {index}: {unknown} not in schema v{self.schema_version}")
            for name, extracted in line.items():
                if extracted.value is None:
                    continue
                if problem := _problem(allowed[name], extracted.value):
                    raise ValueError(f"line {index}: {name}={extracted.value!r} {problem}")
        if self.schema_version >= PROVENANCE_SINCE:
            located = list(self.fields.items())
            located += [
                (f"line {index}: {name}", value)
                for index, line in enumerate(self.line_items, start=1)
                for name, value in line.items()
            ]
            for where, extracted in located:
                if problem := _unproven(extracted):
                    raise ValueError(f"{where} {problem} (schema v{self.schema_version})")
        return self
