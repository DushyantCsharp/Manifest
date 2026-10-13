"""Migration checks that need no database (the Postgres ones are in test_migrations_pg.py)."""

import io
from collections import Counter
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from manifest_db.base import Base
from manifest_db.testing import alembic_config

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
EXPECTED_HISTORY = ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_migration_history_has_single_head() -> None:
    heads = script().get_heads()
    assert len(heads) <= 1, f"multiple Alembic heads {heads}: run `alembic merge`"


def test_history_is_linear_and_numbered() -> None:
    history = [revision.revision for revision in reversed(list(script().walk_revisions()))]
    assert history == EXPECTED_HISTORY


def test_metadata_names_every_constraint_kind() -> None:
    assert set(Base.metadata.naming_convention) >= {"pk", "fk", "uq", "ck", "ix"}


def test_hs_nomenclature_file_is_the_pinned_one() -> None:
    revision = script().get_revision("0002")
    assert revision is not None
    rows = revision.module.load_rows()  # verifies the sha256 first

    assert Counter(row["level"] for row in rows) == {2: 97, 4: 1229, 6: 5613}
    by_code = {row["code"]: row for row in rows}
    assert by_code["847130"]["parent"] == "8471"
    assert by_code["8471"]["parent"] == "84"
    assert by_code["84"]["parent"] is None
    positions = {row["code"]: index for index, row in enumerate(rows)}
    for row in rows:
        if row["parent"] is not None:
            assert positions[row["parent"]] < positions[row["code"]], row["code"]


def test_offline_sql_renders_without_a_database() -> None:
    config = alembic_config("postgresql+asyncpg://offline@localhost/none")
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    sql = config.output_buffer.getvalue()

    assert "CREATE TABLE hs_codes" in sql
    assert "INSERT INTO hs_codes" in sql
    assert "NOT VALID" in sql
    assert "VALIDATE CONSTRAINT ck_line_items_country_of_origin_iso3166" in sql
    assert "VALIDATE CONSTRAINT ck_extractions_provenance_since_v3" in sql
