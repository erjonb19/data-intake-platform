"""14.3: versioned warehouse migrations (dip/migrations.py) on a real Postgres."""

from __future__ import annotations

import pytest

from dip import migrations
from dip.migrations import MigrationChanged
from tests.integration.conftest import rows


def test_a_fresh_warehouse_records_every_migration(warehouse):
    want = [(m.version, m.name, m.sha256) for m in migrations.migrations()]
    got = rows(warehouse, "select version, name, sha256 from ops.schema_migration order by 1")
    assert got == want
    assert migrations.pending(warehouse.conn) == []


def test_a_current_warehouse_applies_nothing(warehouse):
    assert migrations.ensure(warehouse.conn) == []
    assert migrations.migrate(warehouse.conn) == []
    n = rows(warehouse, "select count(*) from ops.schema_migration")[0][0]
    assert n == len(migrations.migrations())


def test_a_warehouse_from_before_migrations_takes_the_baseline_and_keeps_its_data(warehouse):
    """A warehouse built by an earlier release has the tables but no ops.schema_migration:
    0001 is the old idempotent DDL, so it applies over them and nothing is lost."""
    with warehouse.conn.cursor() as cur:
        cur.execute(
            "insert into ops.intake_file values ('abc', 'healthcare.eligibility', 1, 'k', 'r',"
            " 'loaded', 1, 0, 0, now())"
        )
        cur.execute("drop table ops.schema_migration")
    warehouse.conn.commit()
    assert [m.name for m in migrations.pending(warehouse.conn)] == [
        m.name for m in migrations.migrations()
    ]
    applied = migrations.ensure(warehouse.conn)
    assert applied == [m.name for m in migrations.migrations()]
    assert rows(warehouse, "select file_sha256 from ops.intake_file") == [("abc",)]
    assert migrations.pending(warehouse.conn) == []


def test_an_edited_migration_is_refused(warehouse):
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.schema_migration set sha256 = 'edited' where version = 1")
    warehouse.conn.commit()
    with pytest.raises(MigrationChanged, match="add a new migration"):
        migrations.ensure(warehouse.conn)
    with pytest.raises(MigrationChanged):
        migrations.migrate(warehouse.conn)


def test_a_warehouse_ahead_of_this_code_is_not_an_error(warehouse):
    """After a rollback to an older image (13.2) the warehouse has versions this code does
    not know; what this code needs is applied, so nothing fails."""
    with warehouse.conn.cursor() as cur:
        cur.execute("insert into ops.schema_migration values (999, '0999_future.sql', 'x', now())")
    warehouse.conn.commit()
    assert migrations.ensure(warehouse.conn) == []


def test_a_failed_migration_applies_none_of_the_batch(warehouse, monkeypatch):
    with warehouse.conn.cursor() as cur:
        cur.execute("delete from ops.schema_migration where version = 2")
        cur.execute("drop table vocab.source_to_concept_map")
    warehouse.conn.commit()
    broken = migrations.Migration(3, "0003_broken.sql", "select * from no_such_table;")
    real = migrations.migrations()
    monkeypatch.setattr(migrations, "migrations", lambda: [*real, broken])
    with pytest.raises(Exception, match="no_such_table"):
        migrations.migrate(warehouse.conn)
    # 0002 ran first in the same transaction, and was rolled back with 0003
    assert rows(warehouse, "select to_regclass('vocab.source_to_concept_map') is null") == [(True,)]
    assert rows(warehouse, "select max(version) from ops.schema_migration") == [(1,)]


def test_a_login_created_after_the_migrations_gets_its_grants_on_reapply(warehouse):
    """A login the roles Job creates after the migration that defines its grants: the
    deploy Job's reapply_grants gives it what the migration would have (14.3 review)."""
    with warehouse.conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname ="
            " 'dip_platform_check') THEN CREATE ROLE dip_platform_check NOLOGIN; END IF; END $$"
        )
        cur.execute("revoke all on ops.platform_check from dip_platform_check")
    warehouse.conn.commit()
    assert rows(warehouse, "select has_table_privilege('dip_platform_check',"
                " 'ops.platform_check', 'INSERT')") == [(False,)]  # fmt: skip
    names = migrations.reapply_grants(warehouse.conn)
    assert "grant_platform_check" in names
    assert rows(warehouse, "select has_table_privilege('dip_platform_check',"
                " 'ops.platform_check', 'INSERT')") == [(True,)]  # fmt: skip
