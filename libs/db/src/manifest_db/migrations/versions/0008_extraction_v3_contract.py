"""Contract for extraction schema v3: every current extraction carries provenance.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-13

Expand/contract, step 3 of 3 for schema v3. Deploy only after the backfill registered in 0007
(``make backfill-v3``) has re-extracted every current pre-v3 document; the guard below refuses
otherwise. Documents that failed outright are exempt: there is nothing to re-read, and their
old extraction stays as it was. Then the provenance check is validated over every row and new
extractions default to v3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_extractions_provenance_since_v3"

# Same selection as the backfill, minus documents that failed outright.
REMAINING = sa.text(
    """
    SELECT count(*)
    FROM extractions AS e
    JOIN documents AS d ON d.id = e.document_id
    WHERE e.superseded_at IS NULL
      AND e.schema_version < 3
      AND d.status <> 'failed'
    """
)


def upgrade() -> None:
    if not context.is_offline_mode():
        remaining = op.get_bind().execute(REMAINING).scalar_one()
        if remaining:
            raise RuntimeError(
                f"{remaining} document(s) are still on an extraction schema older than v3. "
                "Run `make backfill-v3` until it completes, then upgrade again."
            )
    op.execute(f"ALTER TABLE extractions VALIDATE CONSTRAINT {CHECK}")
    op.alter_column("extractions", "schema_version", server_default=sa.text("3"))


def downgrade() -> None:
    op.alter_column("extractions", "schema_version", server_default=sa.text("2"))
    # Back to the 0007 state: enforced for new writes, not validated.
    op.drop_constraint(op.f(CHECK), "extractions", type_="check")
    op.create_check_constraint(
        op.f(CHECK),
        "extractions",
        "schema_version < 3 OR provenance IS NOT NULL",
        postgresql_not_valid=True,
    )
