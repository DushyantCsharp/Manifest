"""Structured page extraction through the LLM gateway.

The model copies each value exactly as the page prints it and says how sure it is; code then
normalises (decimal separators, dates, currencies, Incoterms, countries, weights to kg). The
same normalisers and confidence rules therefore apply whichever model reads the page, and evals
can tell reading errors from normalisation errors.

Schema v3 (``manifest_domain.schemas.v3``) also asks, for every value, for the verbatim span of
page text it was read from. Code checks the span against the page (``manifest_domain.evidence``;
a value without verified evidence ends with confidence 0), stamps the page number, and records
the signals its confidence is built from (``manifest_domain.scoring``). The v2 request below is
frozen: it serves only v2 pages still in flight.

Context the model isn't asked to judge comes from the page: its decimal separator (inferred
from all its numbers) settles ``1,234``, and day-first dates go with decimal commas. A value
resolved by that context keeps only ``AMBIGUITY_PENALTY`` of its confidence. A value that
won't normalise at all is kept as missing with zero confidence, so that field goes to review
while the rest of the page stands.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from manifest_domain.confidence import stored
from manifest_domain.evidence import check
from manifest_domain.extraction import (
    LINE_FIELDS,
    PROVENANCE_SINCE,
    ExtractedValue,
    Normalised,
    PageFragment,
    Signals,
)
from manifest_domain.lifecycle import DocumentKind
from manifest_domain.normalisers.countries import normalise_country
from manifest_domain.normalisers.currency import normalise_currency
from manifest_domain.normalisers.dates import normalise_date
from manifest_domain.normalisers.incoterms import normalise_incoterm
from manifest_domain.normalisers.numbers import (
    AmbiguityError,
    NormalisationError,
    Separator,
    infer_decimal_separator,
    normalise_weight,
    parse_decimal,
)
from manifest_domain.schemas import v3
from manifest_domain.scoring import AMBIGUITY_PENALTY, page_score
from manifest_llm.gateway import Effort, LlmGateway
from manifest_worker.ports import ExtractedPage, PageInput

Kind = v3.Kind
MODEL = "claude-sonnet-5-5"  # the golden cassettes' model: re-record them when it changes
MAX_TOKENS = 4096  # v2: no thinking budget
MAX_TOKENS_V3 = 16_000  # thinking counts towards it on models that think
EFFORT: Effort = "low"  # copying values needs little reasoning; sweep it on real recordings
PROMPT_VERSION = "2026-10-13"  # the v3 system prompt and field descriptions; recorded per page


@dataclass(frozen=True)
class Spec:
    kind: Kind
    description: str


NUMBER = "number only, exactly as printed (keep its separators; no currency or unit)"
WEIGHT = "weight with its unit, exactly as printed (e.g. '1.739,19 kg', '2,388.9 lb')"
PARTY = "company name only (no address)"

HEADER: dict[DocumentKind, dict[str, Spec]] = {
    DocumentKind.COMMERCIAL_INVOICE: {
        "invoice_number": Spec("text", "Invoice number"),
        "invoice_date": Spec("date", "Invoice date, exactly as printed"),
        "seller_name": Spec("text", f"Seller / exporter: {PARTY}"),
        "buyer_name": Spec("text", f"Buyer / sold to / consignee: {PARTY}"),
        "currency": Spec("currency", "Invoice currency as printed: code, symbol or words"),
        "incoterm": Spec("incoterm", "Delivery terms with the named place, e.g. 'CIF Rotterdam'"),
        "payment_terms": Spec("text", "Payment terms"),
        "total_amount": Spec("decimal", f"Invoice total: {NUMBER}"),
    },
    DocumentKind.PACKING_LIST: {
        "invoice_number": Spec("text", "Invoice number the packing list refers to"),
        "date": Spec("date", "Packing list date, exactly as printed"),
        "seller_name": Spec("text", f"Seller / shipper: {PARTY}"),
        "buyer_name": Spec("text", f"Buyer / consignee: {PARTY}"),
        "incoterm": Spec("incoterm", "Delivery terms with the named place, e.g. 'CIF Rotterdam'"),
        "package_type": Spec("text", "Kind of package, e.g. CARTONS, PALLETS"),
        "total_packages": Spec("integer", f"Total number of packages: {NUMBER}"),
        "total_net_weight_kg": Spec("weight", f"Total net weight: {WEIGHT}"),
        "total_gross_weight_kg": Spec("weight", f"Total gross weight: {WEIGHT}"),
    },
    DocumentKind.BILL_OF_LADING: {
        "bl_number": Spec("text", "Bill of lading number"),
        "seller_name": Spec("text", f"Shipper: {PARTY}"),
        "buyer_name": Spec("text", f"Consignee: {PARTY}"),
        "notify_party_name": Spec("text", f"Notify party: {PARTY}; null if 'same as consignee'"),
        "carrier": Spec("text", "Carrier"),
        "vessel": Spec("text", "Ocean vessel name"),
        "voyage": Spec("text", "Voyage number"),
        "port_of_loading": Spec("text", "Port of loading"),
        "port_of_discharge": Spec("text", "Port of discharge"),
        "shipped_on_board": Spec("date", "Shipped-on-board date, exactly as printed"),
        "container_number": Spec("text", "Container number"),
        "seal_number": Spec("text", "Seal number"),
        "freight_terms": Spec("text", "Freight terms: PREPAID or COLLECT"),
        "package_type": Spec("text", "Kind of package, e.g. CARTONS, PALLETS"),
        "total_packages": Spec("integer", f"Number of packages: {NUMBER}"),
        "total_gross_weight_kg": Spec("weight", f"Gross weight: {WEIGHT}"),
    },
}

LINE: dict[str, Spec] = {
    "description": Spec("text", "Goods description"),
    "quantity": Spec("decimal", f"Quantity: {NUMBER}"),
    "unit": Spec("text", "Unit of measure, e.g. pcs, kg, pairs"),
    "unit_price": Spec("decimal", f"Unit price: {NUMBER}"),
    "amount": Spec("decimal", f"Line total: {NUMBER}"),
    "currency": Spec("currency", "Currency printed on this line, if any"),
    "net_weight_kg": Spec("weight", f"Net weight: {WEIGHT}"),
    "gross_weight_kg": Spec("weight", f"Gross weight: {WEIGHT}"),
    "country_of_origin": Spec("country", "Country of origin"),
}
LINE_COLUMNS: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.COMMERCIAL_INVOICE: (
        "description",
        "quantity",
        "unit",
        "unit_price",
        "amount",
        "currency",
        "country_of_origin",
    ),
    DocumentKind.PACKING_LIST: (
        "description",
        "quantity",
        "unit",
        "net_weight_kg",
        "gross_weight_kg",
        "country_of_origin",
    ),
}

SYSTEM = """You read one page of an international shipping document and record what it prints.

- Copy every value exactly as printed on this page: the same digits, separators, units, date \
format and spelling. Do not convert, reformat, total, correct or infer anything.
- The one exception: give a country of origin as its ISO 3166-1 alpha-2 code ("China" -> "CN").
- A value this page does not print is null. Never carry values over from other pages.
- line_items: one per goods line of the table, in reading order. Skip totals and subtotals.
- confidence: your probability, from 0 to 1, that the text is exactly what is printed and \
belongs to that field. It is checked against ground truth, so be calibrated, not modest or \
optimistic."""


class Reading(BaseModel):
    """One value as the page prints it."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(description="Exactly as printed, or null if not on this page")
    confidence: Decimal = Field(ge=0, le=1, description="Probability that text is right")


def _readings(name: str, specs: dict[str, Spec]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        key: (Reading | None, Field(default=None, description=spec.description))
        for key, spec in specs.items()
    }
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


@cache
def output_model(kind: DocumentKind, schema_version: int) -> type[BaseModel]:
    """What the model must answer for one page of ``kind`` under ``schema_version``."""
    title = "".join(part.title() for part in kind.value.split("_"))
    allowed = LINE_FIELDS[schema_version]
    columns = {k: LINE[k] for k in LINE_COLUMNS.get(kind, ()) if k in allowed}
    line = _readings(f"{title}LineV{schema_version}", columns)
    fields: dict[str, Any] = {
        "fields": (_readings(f"{title}Fields", HEADER.get(kind, {})), ...),
        "line_items": (list[line], ...),  # type: ignore[valid-type]
    }
    return create_model(f"{title}PageV{schema_version}", **fields)


def prompt(page: PageInput, kind: DocumentKind) -> str:
    label = kind.value.replace("_", " ")
    return f"Document: {label}\nPage {page.page_no}\n\nPage text:\n<<<\n{page.text}\n>>>"


# ---------------------------------------------------------------- normalisation


@dataclass(frozen=True)
class PageContext:
    separator: Separator | None
    dollar: str | None = None  # the page's currency, for a bare "$"

    @property
    def day_first(self) -> bool:
        return self.separator != "."


_NOT_NUMERIC = re.compile(r"[^\d.,'\u2019\s]")
_NUMERIC_KINDS = frozenset({"decimal", "integer", "weight"})


def context_for(raw: dict[str, Any], kind: DocumentKind) -> PageContext:
    """The page's conventions, read off every number on it."""
    readings = [(HEADER[kind][name], r) for name, r in raw["fields"].items()]
    readings += [(LINE[name], r) for line in raw["line_items"] for name, r in line.items()]
    texts = [
        _NOT_NUMERIC.sub("", reading["text"])
        for spec, reading in readings
        if reading and reading["text"] and spec.kind in _NUMERIC_KINDS
    ]
    return PageContext(infer_decimal_separator(texts))


def _number(text: str, context: PageContext) -> tuple[Decimal, Decimal]:
    if context.separator is not None:
        return parse_decimal(text, context.separator), Decimal(1)
    try:
        return parse_decimal(text), Decimal(1)
    except AmbiguityError:
        return parse_decimal(text, "."), AMBIGUITY_PENALTY


def _weight(text: str, context: PageContext) -> tuple[Decimal, Decimal]:
    if context.separator is not None:
        return normalise_weight(text, context.separator), Decimal(1)
    try:
        return normalise_weight(text), Decimal(1)
    except AmbiguityError:
        return normalise_weight(text, "."), AMBIGUITY_PENALTY


def normalise(kind: Kind, text: str, context: PageContext) -> tuple[str, Decimal]:
    """(normalised value, confidence factor). Raises ``NormalisationError``."""
    match kind:
        case "text":
            value = " ".join(text.split())
            if not value:
                raise NormalisationError("blank")
            return value, Decimal(1)
        case "decimal":
            number, factor = _number(text, context)
            return str(number), factor
        case "integer":
            number, factor = _number(text, context)
            if number != number.to_integral_value():
                raise NormalisationError(f"{text!r} is not a whole number")
            return str(int(number)), factor
        case "weight":
            kg, factor = _weight(text, context)
            return str(kg), factor
        case "date":
            try:
                return normalise_date(text).isoformat(), Decimal(1)
            except AmbiguityError:
                resolved = normalise_date(text, day_first=context.day_first)
                return resolved.isoformat(), AMBIGUITY_PENALTY
        case "currency":
            return normalise_currency(text, dollar=context.dollar), Decimal(1)
        case "country":
            code = text.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", code):
                raise NormalisationError(f"{text!r} is not an ISO 3166-1 alpha-2 code")
            return code, Decimal(1)
        case "incoterm":
            return normalise_incoterm(text).code, Decimal(1)


def _value(spec: Spec, reading: dict[str, Any], context: PageContext) -> ExtractedValue:
    try:
        value, factor = normalise(spec.kind, reading["text"], context)
    except NormalisationError:
        return ExtractedValue(value=None, confidence=Decimal(0))
    return ExtractedValue(value=value, confidence=stored(Decimal(reading["confidence"]) * factor))


def fragment_from(raw: dict[str, Any], kind: DocumentKind, schema_version: int) -> PageFragment:
    """Normalise the model's readings of one page into a ``PageFragment``."""
    context = context_for(raw, kind)
    fields: dict[str, ExtractedValue] = {}
    for name, reading in raw["fields"].items():
        if not reading or reading["text"] is None:
            continue
        spec = HEADER[kind][name]
        fields[name] = _value(spec, reading, context)
        if spec.kind == "incoterm" and fields[name].value is not None:
            place = normalise_incoterm(reading["text"]).place
            if place is not None:
                fields["incoterm_place"] = fields[name].model_copy(update={"value": place})
    currency = fields.get("currency")
    context = PageContext(context.separator, dollar=currency.value if currency else None)
    lines = []
    for line in raw["line_items"]:
        values = {
            name: _value(LINE[name], reading, context)
            for name, reading in line.items()
            if reading and reading["text"] is not None
        }
        if any(value.value is not None for value in values.values()):
            lines.append(values)
    return PageFragment(schema_version=schema_version, fields=fields, line_items=lines)


# ---------------------------------------------------------------- schema v3

SYSTEM_V3 = """You read one page of an international shipping document and record what it prints.

- For every field give value, evidence and confidence.
- value: exactly as printed on this page: the same digits, separators, units, date format, \
spelling and capitalisation. Do not convert, reformat, total, correct or infer anything. If \
this page does not print the field, value and evidence are both "".
- evidence: the span of page text that shows the value, usually its label and the value, \
copied character for character from the page text (it is checked against the page; a span \
that isn't there counts as unread).
- line_items: one per goods line of the table, in reading order. Skip totals and subtotals.
- confidence: your probability, from 0 to 1, that value is exactly what is printed and \
belongs to that field. It is checked against ground truth, so be calibrated, not modest or \
optimistic."""

V3_LINE_DESCRIPTIONS: dict[str, str] = {
    **{name: spec.description for name, spec in LINE.items()},
    "country_of_origin": "Country of origin exactly as printed (a name or a code)",
}
V3_LINE_ITEMS = "Goods lines of the table, in reading order (not totals or subtotals)"


def _described(
    base: type[BaseModel], descriptions: dict[str, str], **extra: Any
) -> type[BaseModel]:
    """``base`` with this prompt's field descriptions (the schema itself is ``base``)."""
    fields: dict[str, Any] = {}
    for name, info in base.model_fields.items():
        if name in descriptions:
            annotation = Annotated[v3.Reading, *info.metadata]
            fields[name] = (annotation, Field(description=descriptions[name]))
    return create_model(base.__name__, __base__=base, **fields, **extra)


@cache
def request_model(kind: DocumentKind) -> type[BaseModel]:
    """What the model must fill for one page of ``kind`` under schema v3."""
    extra: dict[str, Any] = {}
    if kind in v3.LINES:
        line = _described(v3.LINES[kind], V3_LINE_DESCRIPTIONS)
        extra["line_items"] = (list[line], Field(description=V3_LINE_ITEMS))  # type: ignore[valid-type]
    header = {name: spec.description for name, spec in HEADER[kind].items()}
    return _described(v3.DOCUMENTS[kind], header, **extra)


def normalise_as(kind: Kind, text: str, context: PageContext) -> tuple[str, Normalised]:
    """(normalised value, how): ``ambiguous`` when only the page's conventions decided it.
    Countries may be printed as names. Raises ``NormalisationError``."""
    if kind == "country":
        return normalise_country(text), "exact"
    value, factor = normalise(kind, text, context)
    return value, "exact" if factor == 1 else "ambiguous"


def _context_v3(readings: list[tuple[Kind, dict[str, Any]]]) -> PageContext:
    texts = [
        _NOT_NUMERIC.sub("", reading["value"])
        for kind, reading in readings
        if reading["value"] and kind in _NUMERIC_KINDS
    ]
    return PageContext(infer_decimal_separator(texts))


def _read(
    kind: Kind, reading: dict[str, Any], context: PageContext, page: PageInput
) -> ExtractedValue:
    self_report = Decimal(reading["confidence"])
    try:
        value, normalised = normalise_as(kind, reading["value"], context)
    except NormalisationError:
        value, normalised = None, "failed"
    signals = Signals(
        self_report=self_report,
        evidence=check(page.text, reading["value"], reading["evidence"], kind),
        normaliser=normalised,
    )
    return ExtractedValue(
        value=value,
        confidence=page_score(self_report, normalised),
        page=page.page_no,
        evidence=reading["evidence"] or None,
        signals=signals,
    )


def fragment_v3(answer: dict[str, Any], kind: DocumentKind, page: PageInput) -> PageFragment:
    """Normalise, evidence-check and score the model's v3 readings of one page."""
    header = v3.header_kinds(kind)
    columns = v3.line_kinds(kind)
    printed = [(header[name], answer[name]) for name in header if answer[name]["value"]]
    printed += [
        (columns[name], line[name])
        for line in answer.get("line_items", [])
        for name in columns
        if line[name]["value"]
    ]
    context = _context_v3(printed)
    fields: dict[str, ExtractedValue] = {}
    for name, field_kind in header.items():
        reading = answer[name]
        if not reading["value"].strip():
            continue  # not printed on this page
        fields[name] = _read(field_kind, reading, context, page)
        derived = v3.DERIVED.get(name)
        if derived and fields[name].value is not None:
            place = normalise_incoterm(reading["value"]).place
            if place is not None:
                fields[derived] = fields[name].model_copy(update={"value": place})
    currency = fields.get("currency")
    context = PageContext(context.separator, dollar=currency.value if currency else None)
    lines = []
    for line in answer.get("line_items", []):
        values = {
            name: _read(column_kind, line[name], context, page)
            for name, column_kind in columns.items()
            if line[name]["value"].strip()
        }
        if any(value.value is not None for value in values.values()):
            lines.append(values)
    return PageFragment(schema_version=v3.VERSION, fields=fields, line_items=lines)


# ---------------------------------------------------------------- the port


@dataclass
class LlmPageExtractor:
    gateway: LlmGateway
    provider: str
    model: str
    max_tokens: int | None = None  # None: the schema version's default
    effort: Effort | None = EFFORT

    def extract(self, page: PageInput) -> ExtractedPage:
        kind = page.kind or DocumentKind.COMMERCIAL_INVOICE  # unclassified: read it as one
        if kind not in HEADER:  # nothing we model on this kind of document
            return ExtractedPage(PageFragment(schema_version=page.schema_version), "none", 0, 0)
        if page.schema_version >= PROVENANCE_SINCE:
            return self._extract_v3(page, kind)
        result = self.gateway.structured(
            self.provider,
            self.model,
            system=SYSTEM,
            prompt=prompt(page, kind),
            output=output_model(kind, page.schema_version),
            max_tokens=self.max_tokens or MAX_TOKENS,
        )
        fragment = fragment_from(result.output.model_dump(), kind, page.schema_version)
        model = f"{result.provider}/{result.model}"
        return ExtractedPage(
            fragment, model, result.input_tokens, result.output_tokens, result.latency_ms
        )

    def _extract_v3(self, page: PageInput, kind: DocumentKind) -> ExtractedPage:
        result = self.gateway.structured(
            self.provider,
            self.model,
            system=SYSTEM_V3,
            prompt=prompt(page, kind),
            output=request_model(kind),
            max_tokens=self.max_tokens or MAX_TOKENS_V3,
            effort=self.effort,
        )
        fragment = fragment_v3(result.output.model_dump(), kind, page)
        model = f"{result.provider}/{result.model}"
        return ExtractedPage(
            fragment, model, result.input_tokens, result.output_tokens, result.latency_ms
        )
