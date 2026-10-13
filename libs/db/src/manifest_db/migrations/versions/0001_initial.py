"""Initial schema: intake, extraction, classification and review.

Revision ID: 0001
Revises:
Create Date: 2026-10-12

Frozen snapshot: enum labels and types are spelled out here, never imported from live code.
``line_items.country_of_origin`` arrives in 0003 and ``backfill_runs`` in 0004.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *labels: str) -> postgresql.ENUM:
    return postgresql.ENUM(*labels, name=name, create_type=False)


SHIPMENT_STATUS = _enum(
    "shipment_status", "open", "processing", "needs_review", "approved", "auto_approved"
)
DOCUMENT_KIND = _enum(
    "document_kind", "commercial_invoice", "packing_list", "bill_of_lading", "other"
)
DOCUMENT_STATUS = _enum(
    "document_status",
    "awaiting_upload",
    "queued",
    "processing",
    "extracted",
    "partially_extracted",
    "failed",
)
PAGE_STATUS = _enum("page_status", "pending", "processing", "extracted", "failed")
DISCREPANCY_KIND = _enum(
    "discrepancy_kind",
    "value_mismatch",
    "missing_field",
    "line_items_mismatch",
    "totals_mismatch",
)
DISCREPANCY_SEVERITY = _enum("discrepancy_severity", "info", "warning", "blocking")
REVIEW_TASK_STATUS = _enum("review_task_status", "open", "in_progress", "approved")
REVIEW_REASON = _enum(
    "review_reason",
    "low_confidence",
    "discrepancy",
    "missing_field",
    "partial_extraction",
    "hs_uncertain",
)
CORRECTION_ACTION = _enum("correction_action", "approve", "correct")

ENUMS = (
    SHIPMENT_STATUS,
    DOCUMENT_KIND,
    DOCUMENT_STATUS,
    PAGE_STATUS,
    DISCREPANCY_KIND,
    DISCREPANCY_SEVERITY,
    REVIEW_TASK_STATUS,
    REVIEW_REASON,
    CORRECTION_ACTION,
)

# Dependency order; downgrade drops in reverse.
TABLES = (
    "shipments",
    "documents",
    "pages",
    "extractions",
    "line_items",
    "discrepancies",
    "hs_codes",
    "hs_suggestions",
    "review_tasks",
    "corrections",
)


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    default = None if nullable else sa.text("now()")
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def _jsonb_with_default(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind)

    # ------------------------------------------------------------ intake
    op.create_table(
        "shipments",
        _id(),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("status", SHIPMENT_STATUS, server_default="open", nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
    )
    op.create_index("ix_shipments_keyset", "shipments", ["created_at", "id"])

    op.create_table(
        "documents",
        _id(),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("type", DOCUMENT_KIND, nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("status", DOCUMENT_STATUS, server_default="awaiting_upload", nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_positive")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_sha256_hex")),
        sa.CheckConstraint("page_count > 0", name=op.f("ck_documents_page_count_positive")),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_documents_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_documents_storage_key")),
    )
    op.create_index(op.f("ix_documents_shipment_id"), "documents", ["shipment_id"])

    op.create_table(
        "pages",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("status", PAGE_STATUS, server_default="pending", nullable=False),
        sa.Column("ocr_used", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _timestamp("updated_at"),
        sa.CheckConstraint("page_no >= 1", name=op.f("ck_pages_page_no_positive")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_pages_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_pages_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "page_no", name=op.f("pk_pages")),
    )
    op.create_index(
        "ix_pages_failed", "pages", ["document_id"], postgresql_where=sa.text("status = 'failed'")
    )

    # ------------------------------------------------------------ extraction
    op.create_table(
        "extractions",
        _id(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        # v1 is the only schema so far; 0005 moves the default to 2.
        sa.Column("schema_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _jsonb_with_default("field_confidence", "'{}'::jsonb"),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("superseded_at", nullable=True),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_extractions_schema_version_positive")
        ),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name=op.f("ck_extractions_tokens_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_extractions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extractions")),
    )
    op.create_index(op.f("ix_extractions_document_id"), "extractions", ["document_id"])
    op.create_index(
        "uq_extractions_current_document",
        "extractions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_extractions_current_schema_version",
        "extractions",
        ["schema_version", "document_id"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "line_items",
        _id(),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("net_weight_kg", sa.Numeric(14, 3), nullable=True),
        sa.Column("gross_weight_kg", sa.Numeric(14, 3), nullable=True),
        sa.CheckConstraint("line_no >= 1", name=op.f("ck_line_items_line_no_positive")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_line_items_currency_iso4217")),
        sa.CheckConstraint(
            "(unit_price IS NULL AND amount IS NULL) OR currency IS NOT NULL",
            name=op.f("ck_line_items_money_has_currency"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extractions.id"],
            name=op.f("fk_line_items_extraction_id_extractions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_line_items")),
        sa.UniqueConstraint(
            "extraction_id", "line_no", name=op.f("uq_line_items_extraction_id_line_no")
        ),
    )
    op.create_index(op.f("ix_line_items_extraction_id"), "line_items", ["extraction_id"])

    op.create_table(
        "discrepancies",
        _id(),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("kind", DISCREPANCY_KIND, nullable=False),
        sa.Column("severity", DISCREPANCY_SEVERITY, nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        _jsonb_with_default("observations", "'[]'::jsonb"),
        _timestamp("created_at"),
        _timestamp("resolved_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_discrepancies_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discrepancies")),
    )
    op.create_index(op.f("ix_discrepancies_shipment_id"), "discrepancies", ["shipment_id"])

    # ------------------------------------------------------------ classification
    op.create_table(
        "hs_codes",
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("parent", sa.String(6), nullable=True),
        sa.Column("section", sa.String(5), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(dim=1024), nullable=True),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, description)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN (2, 4, 6) AND length(code) = level AND code ~ '^[0-9]+$'",
            name=op.f("ck_hs_codes_code_matches_level"),
        ),
        sa.CheckConstraint(
            "(level = 2) = (parent IS NULL)", name=op.f("ck_hs_codes_only_chapters_are_roots")
        ),
        sa.ForeignKeyConstraint(
            ["parent"], ["hs_codes.code"], name=op.f("fk_hs_codes_parent_hs_codes")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_hs_codes")),
    )
    op.create_index(op.f("ix_hs_codes_parent"), "hs_codes", ["parent"])
    op.create_index("ix_hs_codes_search", "hs_codes", ["search"], postgresql_using="gin")
    op.create_index(
        "ix_hs_codes_embedding",
        "hs_codes",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "hs_suggestions",
        _id(),
        sa.Column("line_item_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        _jsonb_with_default("retrieved_candidates", "'[]'::jsonb"),
        sa.Column("model", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("length(code) = 6", name=op.f("ck_hs_suggestions_code_is_subheading")),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_hs_suggestions_confidence_is_probability"),
        ),
        sa.ForeignKeyConstraint(
            ["code"], ["hs_codes.code"], name=op.f("fk_hs_suggestions_code_hs_codes")
        ),
        sa.ForeignKeyConstraint(
            ["line_item_id"],
            ["line_items.id"],
            name=op.f("fk_hs_suggestions_line_item_id_line_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hs_suggestions")),
        sa.UniqueConstraint("line_item_id", name=op.f("uq_hs_suggestions_line_item_id")),
    )
    op.create_index(op.f("ix_hs_suggestions_code"), "hs_suggestions", ["code"])

    # ------------------------------------------------------------ review
    op.create_table(
        "review_tasks",
        _id(),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("status", REVIEW_TASK_STATUS, server_default="open", nullable=False),
        sa.Column(
            "reasons",
            postgresql.ARRAY(REVIEW_REASON),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("assignee", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("closed_at", nullable=True),
        sa.CheckConstraint("priority BETWEEN 0 AND 3", name=op.f("ck_review_tasks_priority_range")),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_review_tasks_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_tasks")),
    )
    op.create_index(op.f("ix_review_tasks_shipment_id"), "review_tasks", ["shipment_id"])
    op.create_index(
        "ix_review_tasks_queue",
        "review_tasks",
        ["priority", "created_at", "id"],
        postgresql_where=sa.text("status <> 'approved'"),
    )
    op.create_index(
        "uq_review_tasks_active_shipment",
        "review_tasks",
        ["shipment_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'approved'"),
    )

    op.create_table(
        "corrections",
        _id(),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("action", CORRECTION_ACTION, nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "field_path ~ '^(/[^/]+)+$'", name=op.f("ck_corrections_field_path_is_json_pointer")
        ),
        sa.CheckConstraint(
            "action = 'approve' OR length(btrim(reason)) > 0",
            name=op.f("ck_corrections_correction_has_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extractions.id"],
            name=op.f("fk_corrections_extraction_id_extractions"),
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name=op.f("fk_corrections_review_task_id_review_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corrections")),
    )
    op.create_index(op.f("ix_corrections_extraction_id"), "corrections", ["extraction_id"])
    op.create_index(op.f("ix_corrections_review_task_id"), "corrections", ["review_task_id"])


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind)
    op.execute("DROP EXTENSION IF EXISTS vector")
