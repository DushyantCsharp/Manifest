"""ORM models: the schema as of the latest migration.

Migrations are written from these but never import them (nor the domain enums): a migration is
a frozen snapshot, these are live code. An integration test fails if the two disagree.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from manifest_db.base import Base
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, PageStatus, ShipmentStatus
from manifest_domain.review import (
    CorrectionAction,
    DiscrepancyKind,
    DiscrepancySeverity,
    ReviewPriority,
    ReviewReason,
    ReviewTaskStatus,
)

EMBEDDING_DIMENSIONS = 1024

# Money is an amount plus an ISO 4217 code; quantities and weights are exact decimals.
MONEY = Numeric(18, 2)
UNIT_PRICE = Numeric(18, 6)
QUANTITY = Numeric(18, 4)
WEIGHT_KG = Numeric(14, 3)
CONFIDENCE = Numeric(5, 4)
HS_CODE = String(6)


class BackfillStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


def _pg_enum[E: StrEnum](enum: type[E], name: str) -> SAEnum:
    """A native Postgres enum labelled with the domain enum's values (the wire format)."""
    return SAEnum(
        enum,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class IntEnumType[E: IntEnum](TypeDecorator[E]):
    """An IntEnum stored as smallint, so it sorts and can lead an index."""

    impl = SmallInteger
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | None, dialect: Dialect) -> int | None:
        return None if value is None else int(value)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _optional_timestamp() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- intake


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (Index("ix_shipments_keyset", "created_at", "id"),)

    id: Mapped[uuid.UUID] = _id()
    reference: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ShipmentStatus] = mapped_column(
        _pg_enum(ShipmentStatus, "shipment_status"), server_default=ShipmentStatus.OPEN.value
    )
    reconciled_fingerprint: Mapped[str | None] = mapped_column(Text)
    """The current extractions the shipment was last reconciled against (sha256)."""
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    documents: Mapped[list["Document"]] = relationship(back_populates="shipment", lazy="raise")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("page_count > 0", name="page_count_positive"),
    )

    id: Mapped[uuid.UUID] = _id()
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DocumentKind | None] = mapped_column(
        "type", _pg_enum(DocumentKind, "document_kind")
    )
    """Null until the classify stage has run."""
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[DocumentStatus] = mapped_column(
        _pg_enum(DocumentStatus, "document_status"),
        server_default=DocumentStatus.AWAITING_UPLOAD.value,
    )
    page_count: Mapped[int | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    shipment: Mapped[Shipment] = relationship(back_populates="documents", lazy="raise")
    pages: Mapped[list["Page"]] = relationship(back_populates="document", lazy="raise")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        CheckConstraint("page_no >= 1", name="page_no_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # Partial failure: only failed pages are retried, so find them without a scan.
        Index("ix_pages_failed", "document_id", postgresql_where=text("status = 'failed'")),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[PageStatus] = mapped_column(
        _pg_enum(PageStatus, "page_status"), server_default=PageStatus.PENDING.value
    )
    ocr_used: Mapped[bool | None]
    """Null until text has been extracted; true when the page had no usable text layer."""
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    text: Mapped[str | None] = mapped_column(Text)
    """The page's text layer (native or OCR), kept so re-extraction doesn't repeat OCR."""
    updated_at: Mapped[datetime] = _updated_at()

    document: Mapped[Document] = relationship(back_populates="pages", lazy="raise")


class PageExtraction(Base):
    """One page's extraction result for one schema version. Upserted: reruns overwrite."""

    __tablename__ = "page_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "page_no"], ["pages.document_id", "pages.page_no"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "(status = 'extracted') = (fragment IS NOT NULL)", name="fragment_when_extracted"
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    page_no: Mapped[int] = mapped_column(primary_key=True)
    schema_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    status: Mapped[PageStatus] = mapped_column(_pg_enum(PageStatus, "page_status"))
    fragment: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    """A ``manifest_domain.extraction.PageFragment`` as JSON, when extracted."""
    failure: Mapped[str | None] = mapped_column(Text)
    """Why the page failed: transient_exhausted, invalid_output, rejected, poison, ..."""
    error: Mapped[str | None] = mapped_column(Text)
    needs_review: Mapped[bool] = mapped_column(server_default=text("false"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    model: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------- extraction


class Extraction(Base):
    __tablename__ = "extractions"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="tokens_non_negative"),
        # Enforced for new rows; validated by 0008 once no current v2 extraction remains.
        CheckConstraint("schema_version < 3 OR provenance IS NOT NULL", name="provenance_since_v3"),
        Index(
            "uq_extractions_current_document",
            "document_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        # Finds documents still on an old schema version (drives re-extraction backfills).
        Index(
            "ix_extractions_current_schema_version",
            "schema_version",
            "document_id",
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("3"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    field_confidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    """Per value, its confidence: calibrated (``calibrator``) when one was available."""
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """Schema v3: per value, page, verbatim evidence, the confidence signals and raw score."""
    calibrator: Mapped[str | None] = mapped_column(Text)
    """The calibrator ``field_confidence`` came from (``extraction-v1``); null: raw scores."""
    model: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    created_at: Mapped[datetime] = _created_at()
    superseded_at: Mapped[datetime | None] = _optional_timestamp()
    """Set when a newer extraction of the same document replaces this one."""

    line_items: Mapped[list["LineItem"]] = relationship(back_populates="extraction", lazy="raise")


class LineItem(Base):
    __tablename__ = "line_items"
    __table_args__ = (
        UniqueConstraint("extraction_id", "line_no"),
        CheckConstraint("line_no >= 1", name="line_no_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        CheckConstraint(
            "(unit_price IS NULL AND amount IS NULL) OR currency IS NOT NULL",
            name="money_has_currency",
        ),
        CheckConstraint("country_of_origin ~ '^[A-Z]{2}$'", name="country_of_origin_iso3166"),
    )

    id: Mapped[uuid.UUID] = _id()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int]
    # Everything below is nullable: a value the document doesn't state is flagged by
    # reconciliation, never invented.
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    unit: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[Decimal | None] = mapped_column(UNIT_PRICE)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    net_weight_kg: Mapped[Decimal | None] = mapped_column(WEIGHT_KG)
    gross_weight_kg: Mapped[Decimal | None] = mapped_column(WEIGHT_KG)
    country_of_origin: Mapped[str | None] = mapped_column(CHAR(2))
    """Extraction schema v2. ISO 3166-1 alpha-2; null when the document doesn't state it."""

    extraction: Mapped[Extraction] = relationship(back_populates="line_items", lazy="raise")


class Discrepancy(Base):
    __tablename__ = "discrepancies"
    __table_args__ = (
        # Re-reconciling updates a discrepancy (and keeps its resolution) instead of adding one.
        Index(
            "uq_discrepancies_natural_key",
            "shipment_id",
            "kind",
            "field_key",
            "line_no",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DiscrepancyKind] = mapped_column(_pg_enum(DiscrepancyKind, "discrepancy_kind"))
    severity: Mapped[DiscrepancySeverity] = mapped_column(
        _pg_enum(DiscrepancySeverity, "discrepancy_severity")
    )
    field_key: Mapped[str] = mapped_column(Text)
    line_no: Mapped[int | None]
    message: Mapped[str] = mapped_column(Text)
    observations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    """Per document: document_id, value, page, bbox."""
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = _optional_timestamp()


# ---------------------------------------------------------------- classification


class HsCode(Base):
    """HS 2022 nomenclature: chapters (2 digits), headings (4) and subheadings (6)."""

    __tablename__ = "hs_codes"
    __table_args__ = (
        CheckConstraint(
            "level IN (2, 4, 6) AND length(code) = level AND code ~ '^[0-9]+$'",
            name="code_matches_level",
        ),
        CheckConstraint("(level = 2) = (parent IS NULL)", name="only_chapters_are_roots"),
        Index("ix_hs_codes_search", "search", postgresql_using="gin"),
        Index(
            "ix_hs_codes_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_hs_codes_path_search", "path_search", postgresql_using="gin"),
    )

    code: Mapped[str] = mapped_column(HS_CODE, primary_key=True)
    level: Mapped[int] = mapped_column(SmallInteger)
    parent: Mapped[str | None] = mapped_column(HS_CODE, ForeignKey("hs_codes.code"), index=True)
    section: Mapped[str] = mapped_column(String(5))
    """Roman numeral, I to XXI."""
    description: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(EMBEDDING_DIMENSIONS))
    """Filled by the HS index build (``make hs-index``); null until then."""
    embedding_model: Mapped[str | None] = mapped_column(Text)
    """The model ``embedding`` came from: vectors of different models are never compared."""
    search: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, description)", persisted=True)
    )
    path_search: Mapped[str | None] = mapped_column(TSVECTOR)
    """Full-text over the code (weight A), its heading (B) and chapter (C); filled by 0007."""


class HsNote(Base):
    """A legal note of the nomenclature (section, chapter or heading) the classifier may cite.

    Empty until a licensed source is vendored: the classifier cites nomenclature texts only.
    """

    __tablename__ = "hs_notes"
    __table_args__ = (
        CheckConstraint("scope IN ('section', 'chapter', 'heading')", name="scope_known"),
        CheckConstraint("number >= 1", name="number_positive"),
        UniqueConstraint("scope", "ref", "number"),
    )

    id: Mapped[uuid.UUID] = _id()
    scope: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(Text)
    """Section numeral ("XI"), chapter ("61") or heading ("6109")."""
    number: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)


class HsSuggestion(Base):
    __tablename__ = "hs_suggestions"
    __table_args__ = (
        CheckConstraint("length(code) = 6", name="code_is_subheading"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_probability"),
    )

    id: Mapped[uuid.UUID] = _id()
    line_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("line_items.id", ondelete="CASCADE"), unique=True
    )
    code: Mapped[str] = mapped_column(HS_CODE, ForeignKey("hs_codes.code"), index=True)
    confidence: Mapped[Decimal] = mapped_column(CONFIDENCE)
    """Calibrated (``calibrator``) when one was available, else the raw score."""
    raw_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    calibrator: Mapped[str | None] = mapped_column(Text)
    walk: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """Chapter, heading and subheading steps: options shown, choice, citations, confidence."""
    rationale: Mapped[str] = mapped_column(Text)
    retrieved_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    """Best first: code, description, retrieval score."""
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------- review


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint("priority BETWEEN 0 AND 3", name="priority_range"),
        # The queue: keyset-paginated by (priority, created_at, id) over unfinished tasks.
        Index(
            "ix_review_tasks_queue",
            "priority",
            "created_at",
            "id",
            postgresql_where=text("status <> 'approved'"),
        ),
        Index(
            "uq_review_tasks_active_shipment",
            "shipment_id",
            unique=True,
            postgresql_where=text("status <> 'approved'"),
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True
    )
    priority: Mapped[ReviewPriority] = mapped_column(IntEnumType(ReviewPriority))
    status: Mapped[ReviewTaskStatus] = mapped_column(
        _pg_enum(ReviewTaskStatus, "review_task_status"),
        server_default=ReviewTaskStatus.OPEN.value,
    )
    reasons: Mapped[list[ReviewReason]] = mapped_column(
        ARRAY(_pg_enum(ReviewReason, "review_reason")), server_default=text("'{}'")
    )
    assignee: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    closed_at: Mapped[datetime | None] = _optional_timestamp()


class Correction(Base):
    """A reviewer's verdict on one extracted field. Append-only: these are eval labels."""

    __tablename__ = "corrections"
    __table_args__ = (
        CheckConstraint("field_path ~ '^(/[^/]+)+$'", name="field_path_is_json_pointer"),
        CheckConstraint(
            "action = 'approve' OR length(btrim(reason)) > 0", name="correction_has_reason"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extractions.id"), index=True)
    field_path: Mapped[str] = mapped_column(Text)
    """JSON Pointer into the extraction payload, e.g. ``/line_items/2/quantity``."""
    action: Mapped[CorrectionAction] = mapped_column(
        _pg_enum(CorrectionAction, "correction_action")
    )
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    reviewer: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------- operations


class BackfillRun(Base):
    """Checkpoint for a resumable data backfill (one row per backfill)."""

    __tablename__ = "backfill_runs"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[BackfillStatus] = mapped_column(
        _pg_enum(BackfillStatus, "backfill_status"), server_default=BackfillStatus.PENDING.value
    )
    cursor: Mapped[str | None] = mapped_column(Text)
    """Opaque keyset position of the last item processed; the backfill defines its format."""
    processed: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    started_at: Mapped[datetime | None] = _optional_timestamp()
    updated_at: Mapped[datetime] = _updated_at()
    completed_at: Mapped[datetime | None] = _optional_timestamp()
