"""GraphQL types for the reviewer UI: the shapes recorded in contracts/schema.graphql."""

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Annotated

import strawberry

from manifest_domain.lifecycle import DocumentKind
from manifest_domain.review import (
    DiscrepancyKind,
    DiscrepancySeverity,
    FieldStatus,
    ReviewPriority,
    ReviewReason,
    ReviewTaskStatus,
)

# Expose the domain enums as GraphQL enums (registers them in place; values are member names).
strawberry.enum(DocumentKind)
strawberry.enum(ReviewTaskStatus)
strawberry.enum(ReviewPriority, description="URGENT is reviewed first.")
strawberry.enum(ReviewReason)
strawberry.enum(FieldStatus)
strawberry.enum(DiscrepancyKind)
strawberry.enum(DiscrepancySeverity)


# ---------------------------------------------------------------- values


@strawberry.type
class TextValue:
    text: str


@strawberry.type(description="An amount of money. Never a bare number: always with its currency.")
class MoneyValue:
    amount: Decimal
    currency: str = strawberry.field(description="ISO 4217 code, e.g. `USD`.")


@strawberry.type
class QuantityValue:
    amount: Decimal
    unit: str = strawberry.field(description="Normalised unit; weights are always `kg`.")


@strawberry.type
class DateValue:
    date: dt.date


FieldValueType = TextValue | MoneyValue | QuantityValue | DateValue
FieldValue = Annotated[FieldValueType, strawberry.union("FieldValue")]


@strawberry.type(
    description=(
        "Region on the source page. Edges are fractions (0-1) of the page's width or height, "
        "measured from the top-left corner, so they don't depend on render resolution."
    )
)
class BoundingBox:
    left: float
    top: float
    right: float
    bottom: float


# ---------------------------------------------------------------- extraction


@strawberry.type
class FieldCorrection:
    previous_value: FieldValue | None
    reason: str
    corrected_at: dt.datetime


@strawberry.type(description="One extracted value with the evidence a reviewer needs to check it.")
class ExtractedField:
    id: strawberry.ID
    key: str = strawberry.field(description="Schema field name, e.g. `invoice_number`.")
    value: FieldValue | None = strawberry.field(
        description="Current value: the correction if corrected. Null when missing."
    )
    confidence: float = strawberry.field(
        description="Calibrated probability (0-1) that the extracted value is correct."
    )
    source_page: int | None = strawberry.field(
        description="1-based page of the source document the value was read from."
    )
    bbox: BoundingBox | None
    status: FieldStatus
    correction: FieldCorrection | None


@strawberry.type
class HsCandidate:
    code: str
    description: str
    score: float = strawberry.field(description="Hybrid retrieval score; higher is closer.")


@strawberry.type
class HsSuggestion:
    code: str = strawberry.field(description="6-digit HS 2022 subheading, e.g. `847130`.")
    description: str
    confidence: float = strawberry.field(
        description="Calibrated probability (0-1) that `code` is the correct subheading."
    )
    rationale: str = strawberry.field(description="Why this code, citing retrieved candidates.")
    retrieved_candidates: list[HsCandidate] = strawberry.field(
        description="Nomenclature entries retrieved as evidence, best first."
    )


@strawberry.type
class LineItem:
    line_number: int
    fields: list[ExtractedField]
    hs_suggestion: HsSuggestion | None


@strawberry.type
class Extraction:
    id: strawberry.ID
    document_id: strawberry.ID
    document_kind: DocumentKind
    schema_version: int
    fields: list[ExtractedField]
    line_items: list[LineItem]


@strawberry.type
class DiscrepancyObservation:
    document_id: strawberry.ID
    document_kind: DocumentKind
    value: FieldValue | None
    source_page: int | None
    bbox: BoundingBox | None


@strawberry.type(description="A cross-document disagreement found during reconciliation.")
class Discrepancy:
    id: strawberry.ID
    kind: DiscrepancyKind
    severity: DiscrepancySeverity
    field_key: str
    line_number: int | None
    message: str
    observations: list[DiscrepancyObservation]


# ---------------------------------------------------------------- review queue


@strawberry.type
class ReviewTask:
    id: strawberry.ID
    shipment_id: strawberry.ID
    status: ReviewTaskStatus
    priority: ReviewPriority
    reasons: list[ReviewReason]
    created_at: dt.datetime
    extractions: list[Extraction]
    discrepancies: list[Discrepancy]


@strawberry.type
class PageInfo:
    has_next_page: bool
    has_previous_page: bool = strawberry.field(
        description="Always false: keyset connections here page forward only (`first`/`after`)."
    )
    start_cursor: str | None
    end_cursor: str | None


@strawberry.type
class ReviewTaskEdge:
    cursor: str
    node: ReviewTask


@strawberry.type(description="Ordered by priority, then oldest first. Keyset-paginated.")
class ReviewTaskConnection:
    edges: list[ReviewTaskEdge]
    page_info: PageInfo


@strawberry.input
class ReviewTaskFilter:
    status: list[ReviewTaskStatus] | None = None
    priority: list[ReviewPriority] | None = None


# ---------------------------------------------------------------- mutations


@strawberry.input
class MoneyInput:
    amount: Decimal
    currency: str = strawberry.field(description="ISO 4217 code, e.g. `USD`.")


@strawberry.input
class QuantityInput:
    amount: Decimal
    unit: str


@strawberry.input(one_of=True, description="Exactly one member must be set.")
class FieldValueInput:
    text: str | None = strawberry.UNSET
    money: MoneyInput | None = strawberry.UNSET
    quantity: QuantityInput | None = strawberry.UNSET
    date: dt.date | None = strawberry.UNSET


@strawberry.enum
class UserErrorCode(Enum):
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_VALUE = "invalid_value"


@strawberry.type(description="An expected failure, returned as data rather than a GraphQL error.")
class UserError:
    code: UserErrorCode
    message: str
    field: list[str] | None = strawberry.field(
        description='Path to the offending argument, e.g. `["value", "money", "currency"]`.'
    )


@strawberry.type
class ApproveFieldPayload:
    field: ExtractedField | None
    user_errors: list[UserError]


@strawberry.type
class CorrectFieldPayload:
    field: ExtractedField | None
    user_errors: list[UserError]


@strawberry.type
class ApproveTaskPayload:
    task: ReviewTask | None
    user_errors: list[UserError]
