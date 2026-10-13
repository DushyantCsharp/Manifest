"""Contract for extraction schema v2: validate origin format, default new rows to v2.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12

Expand/contract, step 3 of 3. Deploy only after the backfill registered in 0004 has finished;
the guard below refuses to apply otherwise. Origin stays nullable: a v2 line whose documents
don't state an origin keeps NULL and reconciliation flags it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_line_items_country_of_origin_iso3166"

# Same selection as the backfill: current v1 extractions with any line lacking an origin.
REMAINING = sa.text(
    """
    SELECT count(DISTINCT e.document_id)
    FROM extractions AS e
    JOIN line_items AS li ON li.extraction_id = e.id
    WHERE e.superseded_at IS NULL
      AND e.schema_version = 1
      AND li.country_of_origin IS NULL
    """
)


def upgrade() -> None:
    if not context.is_offline_mode():
        remaining = op.get_bind().execute(REMAINING).scalar_one()
        if remaining:
            raise RuntimeError(
                f"{remaining} v1 document(s) still have line items without country_of_origin. "
                "Run `make backfill-origin` until it completes, then upgrade again."
            )
    op.execute(f"ALTER TABLE line_items VALIDATE CONSTRAINT {CHECK}")
    op.alter_column("extractions", "schema_version", server_default=sa.text("2"))


def downgrade() -> None:
    op.alter_column("extractions", "schema_version", server_default=sa.text("1"))
    # Back to the 0003 state: enforced for new writes, not validated.
    op.drop_constraint(op.f(CHECK), "line_items", type_="check")
    op.create_check_constraint(
        op.f(CHECK),
        "line_items",
        "country_of_origin ~ '^[A-Z]{2}$'",
        postgresql_not_valid=True,
    )
