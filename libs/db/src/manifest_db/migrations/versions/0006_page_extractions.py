"""Per-page extraction results, keyed by (page, schema version); page text.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12

``page_extractions`` is what page tasks write: one row per (document, page, schema version),
upserted, so a rerun overwrites rather than duplicates. ``pages.text`` keeps the text layer
(native or OCR) so re-extraction into a new schema version doesn't repeat OCR. Additive only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAGE_STATUS = postgresql.ENUM(name="page_status", create_type=False)  # created in 0001


def upgrade() -> None:
    op.add_column("pages", sa.Column("text", sa.Text(), nullable=True))
    op.create_table(
        "page_extractions",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("status", PAGE_STATUS, nullable=False),
        sa.Column("fragment", postgresql.JSONB(), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'extracted') = (fragment IS NOT NULL)",
            name=op.f("ck_page_extractions_fragment_when_extracted"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "page_no"],
            ["pages.document_id", "pages.page_no"],
            name=op.f("fk_page_extractions_document_id_pages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "page_no", "schema_version", name=op.f("pk_page_extractions")
        ),
    )


def downgrade() -> None:
    op.drop_table("page_extractions")
    op.drop_column("pages", "text")
