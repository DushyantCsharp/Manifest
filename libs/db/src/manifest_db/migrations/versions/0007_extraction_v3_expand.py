"""Expand for extraction schema v3 (provenance), calibrated confidences, cross-document
reconciliation and HS retrieval.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13

Expand/contract, step 1 of 3 for schema v3. Additive only, so the running v2 code is unaffected:

- ``extractions.provenance``: per value, the page it was read from, the verbatim evidence span
  and the confidence signals; required for v3 rows by a check that is enforced for new writes
  now and validated by 0008 once the v3 backfill has re-extracted every current v2 document.
  ``extractions.calibrator`` names the calibrator its confidences came from.
- ``shipments.reconciled_fingerprint``: which set of current extractions the shipment was last
  reconciled against, so a repeated or stale reconciliation does nothing.
- A natural key on ``discrepancies`` (shipment, kind, field, line), so re-reconciling updates a
  discrepancy (and keeps a reviewer's resolution) instead of adding a duplicate.
- ``hs_codes.embedding_model`` (vectors from different models are never compared) and
  ``hs_codes.path_search``: full-text search over a code and its ancestors (subheading A,
  heading B, chapter C), filled here from data already in the table.
- ``hs_suggestions``: the raw (uncalibrated) confidence, the calibrator, and the walk.
- ``hs_notes``: legal notes (section, chapter, heading) for the classifier to cite; empty until a
  licensed source is vendored.
- Registers the v3 re-extraction backfill (run out of band: ``make backfill-v3``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = "extractions.schema_v3"
PROVENANCE_CHECK = "ck_extractions_provenance_since_v3"

backfill_runs = sa.table("backfill_runs", sa.column("name", sa.Text))

# A code's own text weighs most, then its heading's, then its chapter's.
FILL_PATH_SEARCH = """
UPDATE hs_codes AS code
SET path_search = setweight(to_tsvector('english'::regconfig, code.description), 'A')
    || CASE
        WHEN code.level = 6 THEN
            setweight(to_tsvector('english'::regconfig, heading.description), 'B')
            || setweight(to_tsvector('english'::regconfig, chapter.description), 'C')
        WHEN code.level = 4 THEN
            setweight(to_tsvector('english'::regconfig, chapter.description), 'B')
        ELSE ''::tsvector
    END
FROM hs_codes AS self
LEFT JOIN hs_codes AS heading ON self.level = 6 AND heading.code = left(self.code, 4)
LEFT JOIN hs_codes AS chapter ON self.level > 2 AND chapter.code = left(self.code, 2)
WHERE self.code = code.code
"""


def upgrade() -> None:
    op.add_column("extractions", sa.Column("provenance", postgresql.JSONB(), nullable=True))
    op.add_column("extractions", sa.Column("calibrator", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(PROVENANCE_CHECK),
        "extractions",
        "schema_version < 3 OR provenance IS NOT NULL",
        postgresql_not_valid=True,
    )
    op.add_column("shipments", sa.Column("reconciled_fingerprint", sa.Text(), nullable=True))
    op.create_index(
        op.f("uq_discrepancies_natural_key"),
        "discrepancies",
        ["shipment_id", "kind", "field_key", "line_no"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.add_column("hs_codes", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("hs_codes", sa.Column("path_search", postgresql.TSVECTOR(), nullable=True))
    op.execute(FILL_PATH_SEARCH)
    op.create_index(
        op.f("ix_hs_codes_path_search"), "hs_codes", ["path_search"], postgresql_using="gin"
    )

    op.add_column("hs_suggestions", sa.Column("raw_confidence", sa.Numeric(5, 4), nullable=True))
    op.add_column("hs_suggestions", sa.Column("calibrator", sa.Text(), nullable=True))
    op.add_column("hs_suggestions", sa.Column("walk", postgresql.JSONB(), nullable=True))

    op.create_table(
        "hs_notes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("number", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('section', 'chapter', 'heading')", name=op.f("ck_hs_notes_scope_known")
        ),
        sa.CheckConstraint("number >= 1", name=op.f("ck_hs_notes_number_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hs_notes")),
        sa.UniqueConstraint("scope", "ref", "number", name=op.f("uq_hs_notes_scope_ref_number")),
    )
    op.bulk_insert(backfill_runs, [{"name": BACKFILL}])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM backfill_runs WHERE name = :name").bindparams(name=BACKFILL))
    op.drop_table("hs_notes")
    op.drop_column("hs_suggestions", "walk")
    op.drop_column("hs_suggestions", "calibrator")
    op.drop_column("hs_suggestions", "raw_confidence")
    op.drop_index(op.f("ix_hs_codes_path_search"), table_name="hs_codes")
    op.drop_column("hs_codes", "path_search")
    op.drop_column("hs_codes", "embedding_model")
    op.drop_index(op.f("uq_discrepancies_natural_key"), table_name="discrepancies")
    op.drop_column("shipments", "reconciled_fingerprint")
    op.drop_constraint(op.f(PROVENANCE_CHECK), "extractions", type_="check")
    op.drop_column("extractions", "calibrator")
    op.drop_column("extractions", "provenance")
