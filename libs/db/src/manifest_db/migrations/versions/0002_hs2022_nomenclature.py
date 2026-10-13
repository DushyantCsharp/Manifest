"""Load the HS 2022 nomenclature: chapters, headings and subheadings.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12

Data: ``migrations/data/hs2022/harmonized-system.csv`` (provenance in ``SOURCE.md``). The
checksum pins the file: another nomenclature edition is a new file and a new migration, never
an edit to this one.
"""

import csv
import hashlib
import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATA = Path(__file__).resolve().parents[1] / "data" / "hs2022" / "harmonized-system.csv"
SHA256 = "e77ee9e1cfd15a42d1fadeb2c0618d10f6b9ed9aec0e5d66826dc7ef9d3126a5"
ROOT = "TOTAL"  # pseudo-row for "all commodities"; also the parent of every chapter

hs_codes = sa.table(
    "hs_codes",
    sa.column("code", sa.String),
    sa.column("level", sa.SmallInteger),
    sa.column("parent", sa.String),
    sa.column("section", sa.String),
    sa.column("description", sa.Text),
)


def load_rows() -> list[dict[str, Any]]:
    """Parse the pinned CSV into hs_codes rows, parents before children."""
    raw = DATA.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"{DATA.name} has sha256 {digest}, expected {SHA256}; never edit it")
    rows = [
        {
            "code": record["hscode"],
            "level": int(record["level"]),
            "parent": None if record["parent"] == ROOT else record["parent"],
            "section": record["section"],
            "description": record["description"],
        }
        for record in csv.DictReader(io.StringIO(raw.decode("utf-8")))
        if record["hscode"] != ROOT
    ]
    return sorted(rows, key=lambda row: (row["level"], row["code"]))


def upgrade() -> None:
    op.bulk_insert(hs_codes, load_rows())


def downgrade() -> None:
    # Fails (FK) if suggestions still reference codes: deleting reference data under live
    # classifications must be a deliberate, separate step.
    op.execute(hs_codes.delete())
