"""Extraction schema v3: one Pydantic model per document type, filled by the model per page.

Every field is a ``Reading``: the value exactly as printed, the verbatim span of page text the
value was read from (``evidence``), and the model's confidence. Code normalises the value
(``manifest_domain.normalisers``), checks the evidence against the page
(``manifest_domain.evidence``) and stamps the page number, so each extracted value carries its
provenance. What is extracted (field names, their normalisation kinds, and that every value
has evidence) is frozen here; how the model is asked (descriptions, system prompt) belongs to
the extractor and is versioned there, so rewording a prompt is not a new schema.

Every property is required and nothing is nullable: ``""`` means "not printed on this page".
Structured outputs cap optional and union-typed properties per request, and this keeps the
schema well inside both.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from manifest_domain.lifecycle import DocumentKind

VERSION = 3

Kind = Literal["text", "decimal", "integer", "date", "currency", "incoterm", "weight", "country"]


@dataclass(frozen=True)
class As:
    """How a field's printed value is normalised (``kinds`` reads it back)."""

    kind: Kind


class Reading(BaseModel):
    """One value as one page prints it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(description='Exactly as printed on this page; "" if it is not printed.')
    evidence: str = Field(
        description=(
            "The shortest span of the page text, copied character for character, that shows"
            ' the value (usually its label and the value); "" when value is "".'
        )
    )
    # A plain number in the schema: structured outputs reject numeric bounds, so the bounds
    # are enforced on the answer instead.
    confidence: Annotated[Decimal, WithJsonSchema({"type": "number"})] = Field(
        ge=0, le=1, description="Probability, 0 to 1, that value is exactly what is printed."
    )


Text = Annotated[Reading, As("text")]
Number = Annotated[Reading, As("decimal")]
Count = Annotated[Reading, As("integer")]
Day = Annotated[Reading, As("date")]
Currency = Annotated[Reading, As("currency")]
Incoterm = Annotated[Reading, As("incoterm")]
Weight = Annotated[Reading, As("weight")]
Country = Annotated[Reading, As("country")]

_STRICT = ConfigDict(extra="forbid", frozen=True)


class CommercialInvoiceLine(BaseModel):
    model_config = _STRICT

    description: Text
    quantity: Number
    unit: Text
    unit_price: Number
    amount: Number
    currency: Currency
    country_of_origin: Country


class PackingListLine(BaseModel):
    model_config = _STRICT

    description: Text
    quantity: Number
    unit: Text
    net_weight_kg: Weight
    gross_weight_kg: Weight
    country_of_origin: Country


class CommercialInvoice(BaseModel):
    model_config = _STRICT

    invoice_number: Text
    invoice_date: Day
    seller_name: Text
    buyer_name: Text
    currency: Currency
    incoterm: Incoterm
    payment_terms: Text
    total_amount: Number
    line_items: list[CommercialInvoiceLine]


class PackingList(BaseModel):
    model_config = _STRICT

    invoice_number: Text
    date: Day
    seller_name: Text
    buyer_name: Text
    incoterm: Incoterm
    package_type: Text
    total_packages: Count
    total_net_weight_kg: Weight
    total_gross_weight_kg: Weight
    line_items: list[PackingListLine]


class BillOfLading(BaseModel):
    model_config = _STRICT

    bl_number: Text
    seller_name: Text
    buyer_name: Text
    notify_party_name: Text
    carrier: Text
    vessel: Text
    voyage: Text
    port_of_loading: Text
    port_of_discharge: Text
    shipped_on_board: Day
    container_number: Text
    seal_number: Text
    freight_terms: Text
    package_type: Text
    total_packages: Count
    total_gross_weight_kg: Weight


DOCUMENTS: dict[DocumentKind, type[BaseModel]] = {
    DocumentKind.COMMERCIAL_INVOICE: CommercialInvoice,
    DocumentKind.PACKING_LIST: PackingList,
    DocumentKind.BILL_OF_LADING: BillOfLading,
}
LINES: dict[DocumentKind, type[BaseModel]] = {
    DocumentKind.COMMERCIAL_INVOICE: CommercialInvoiceLine,
    DocumentKind.PACKING_LIST: PackingListLine,
}
DERIVED = {"incoterm": "incoterm_place"}
"""Fields normalisation splits in two: ``CIF Rotterdam`` is ``CIF`` at place ``Rotterdam``."""


def kinds(model: type[BaseModel]) -> dict[str, Kind]:
    """Each ``Reading`` field of ``model`` and how its value is normalised."""
    found: dict[str, Kind] = {}
    for name, info in model.model_fields.items():
        for marker in info.metadata:
            if isinstance(marker, As):
                found[name] = marker.kind
    return found


def header_kinds(kind: DocumentKind) -> dict[str, Kind]:
    return kinds(DOCUMENTS[kind]) if kind in DOCUMENTS else {}


def line_kinds(kind: DocumentKind) -> dict[str, Kind]:
    return kinds(LINES[kind]) if kind in LINES else {}
