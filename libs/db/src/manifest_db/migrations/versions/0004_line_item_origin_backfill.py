"""Backfill for extraction schema v2: register the country-of-origin re-extraction.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12

Expand/contract, step 2 of 3. Re-extraction calls the LLM and can take hours, so it never runs
inside a migration. This creates the checkpoint table and registers the run; the worker
executes it out of band (``make backfill-origin``), and 0005 refuses to apply until it's done.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = "line_items.country_of_origin"
BACKFILL_STATUS = postgresql.ENUM(
    "pending", "running", "completed", name="backfill_status", create_type=False
)

backfill_runs = sa.table("backfill_runs", sa.column("name", sa.Text))


def upgrade() -> None:
    BACKFILL_STATUS.create(op.get_bind())
    op.create_table(
        "backfill_runs",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", BACKFILL_STATUS, server_default="pending", nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("processed", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_backfill_runs")),
    )
    op.bulk_insert(backfill_runs, [{"name": BACKFILL}])


def downgrade() -> None:
    op.drop_table("backfill_runs")
    BACKFILL_STATUS.drop(op.get_bind())
