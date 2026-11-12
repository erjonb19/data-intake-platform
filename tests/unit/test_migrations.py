"""14.3: the migration files themselves, with no database."""

from __future__ import annotations

from pathlib import Path

from dip import migrations
from dip.vocab import STCM_DDL

ROOT = Path(__file__).resolve().parents[2]


def test_files_are_numbered_from_one_with_no_gaps():
    ms = migrations.migrations()
    assert [m.version for m in ms] == list(range(1, len(ms) + 1))
    assert ms[0].name == "0001_baseline.sql"


def test_the_stcm_migration_matches_the_vocabulary_loader():
    m = {x.version: x for x in migrations.migrations()}[2]
    assert STCM_DDL.strip() in m.sql


def test_texts_are_read_with_lf_line_ends():
    # A CRLF checkout (Windows) must hash the same as the LF one the cluster applied.
    assert all("\r" not in m.sql for m in migrations.migrations())


def test_the_old_schema_file_is_gone():
    # One source of truth: the schema is the migrations, not a file re-applied on every task.
    assert not (ROOT / "dip" / "sql" / "warehouse.sql").exists()


def test_migrations_ship_in_the_package():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"sql/migrations/*.sql"' in text
