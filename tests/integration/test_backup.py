"""Stage 14 (14.1): the dip_backup login records its run in ops.backup, reads every table
(pg_read_all_data, for pg_dump) and writes nothing else; the exporter shows the newest good
backup's time, and no series before the first."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from dip import backup
from dip.contracts import REPO_ROOT
from dip.metrics import QUERIES
from tests.integration.conftest import rows
from tests.unit.test_backup import FakeStore, work

T0 = datetime(2026, 11, 9, 2, 15, 3, tzinfo=UTC)


def backup_login(warehouse) -> dict:
    with warehouse.conn.cursor() as cur:
        cur.execute((REPO_ROOT / "infra/terraform/files/postgres-roles.sql").read_text("utf-8"))
        cur.execute("alter role dip_backup password 'ci-backup-0123'")
    warehouse.conn.commit()
    warehouse.init()  # the role exists now: its grants apply
    info = psycopg.conninfo.conninfo_to_dict(warehouse.conn.info.dsn)
    info.update(user="dip_backup", password="ci-backup-0123")
    return info


def test_the_backup_login_records_its_run_reads_all_and_writes_nothing_else(warehouse, tmp_path):
    with psycopg.connect(**backup_login(warehouse)) as conn:
        out = backup.run(conn, FakeStore(), work(tmp_path), now=lambda: T0)
        assert out["status"] == "ok", out
        with conn.cursor() as cur:  # what pg_dump needs: every table, ops_secret too
            cur.execute("select count(*) from ops_secret.deid_key")
            cur.execute("select count(*) from ops.approvals")
        conn.rollback()
        for sql in ("insert into ops.incident (feed, kind, detail) values ('x', 'y', 'z')",
                    "delete from ops.backup", "update ops.backup set started_at = now()",
                    "create table ops.x (i int)"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(sql)
            conn.rollback()
    [(status, key, size)] = rows(warehouse, "select status, object_key, bytes from ops.backup")
    assert status == "ok" and key == "warehouse/warehouse-20261109T021503Z.dump" and size == 13


def test_the_exporter_shows_only_a_good_backup(warehouse, tmp_path):
    sql = QUERIES["dip_backup_last_success_timestamp_seconds"][2]
    assert rows(warehouse, sql) == []  # no backup yet: no series, not 0
    backup.run(warehouse.conn, FakeStore(), work(tmp_path, "failed", None, "boom"),
               now=lambda: T0)  # fmt: skip
    assert rows(warehouse, sql) == []  # a failed one does not count
    backup.run(warehouse.conn, FakeStore(), work(tmp_path), now=lambda: T0)
    [(epoch,)] = rows(warehouse, sql)
    assert float(epoch) == T0.timestamp()
