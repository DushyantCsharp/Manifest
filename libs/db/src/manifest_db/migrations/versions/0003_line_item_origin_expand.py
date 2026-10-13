"""Expand for extraction schema v2: nullable ``line_items.country_of_origin``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12

Expand/contract, step 1 of 3. Safe while v1 code still runs: the column is nullable with no
default (a catalog-only change, no table rewrite). The format check is added NOT VALID, so
every new write is checked immediately without scanning existing rows; 0005 validates it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_line_items_country_of_origin_iso3166"


def upgrade() -> None:
    op.add_column("line_items", sa.Column("country_of_origin", sa.CHAR(2), nullable=True))
    op.create_check_constraint(
        op.f(CHECK),
        "line_items",
        "country_of_origin ~ '^[A-Z]{2}$'",
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK), "line_items", type_="check")
    op.drop_column("line_items", "country_of_origin")
