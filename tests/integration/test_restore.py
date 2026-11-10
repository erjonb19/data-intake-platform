"""Stage 14 (14.2): the restore drill's SQL against Postgres, as psql runs it in the pod.
The snapshot and the drop happen together or not at all; the checksum ignores row order
and sees any changed row; the snapshot keeps the owner and grants the restore compares."""

from __future__ import annotations

import psycopg
import pytest

from dip import restore
from tests.integration.conftest import DSN

TABLE = restore.DRILL_TABLE
ROWS = [
    ("M000000001", "HMO", "1980-01-02", True, "2026-11-01 10:00:00+00", 12.5),
    ("M000000002", "PPO", "1975-06-30", False, "2026-11-02 11:30:00+02", None),
    ("M000000003", "EPO", "2001-12-31", True, None, 0.1),
]


def results(conn, sql: str) -> list[tuple]:
    """The rows of the last statement that returned any (psql prints those)."""
    out: list[tuple] = []
    with conn.cursor() as cur:
        cur.execute(sql)
        while True:
            if cur.description:
                out = cur.fetchall()
            if not cur.nextset():
                break
    return out


def make_table(conn, rows) -> None:
    with conn.cursor() as cur:
        cur.execute(f"create table {TABLE} (member_id text, plan_code text, dob date,"
                    " in_latest_file boolean, received_at timestamptz,"
                    " premium double precision)")  # fmt: skip
        for r in rows:
            cur.execute(f"insert into {TABLE} values (%s, %s, %s, %s, %s, %s)", r)
        cur.execute(f"grant select on {TABLE} to public")


@pytest.fixture
def conn(warehouse):
    with psycopg.connect(DSN, autocommit=True) as c:  # as psql: each statement its own
        c.execute("create schema if not exists gold")  # dbt makes it in the cluster
        make_table(c, ROWS)
        key = "warehouse/warehouse-20261109T021503Z.dump"
        c.execute("insert into ops.backup (started_at, finished_at, status, object_key)"
                  " values (now(), now(), 'ok', %s)", (key,))  # fmt: skip
        yield c


def test_the_drill_snapshots_and_drops_in_one_go_and_the_checksum_ignores_order(conn):
    ((n, checksum),) = results(conn, restore.checksum_sql(TABLE))
    assert n == 3 and len(checksum) == 32
    backup_id = results(conn, "select max(backup_id) from ops.backup")[0][0]
    assert results(conn, restore.precheck_sql()) == [(True, "", str(backup_id))]

    ((drill_id, rows, snap, backup),) = results(conn, restore.drop_sql())
    assert (rows, snap, backup) == (3, checksum, str(backup_id))
    assert results(conn, f"select to_regclass('{TABLE}')") == [(None,)]
    assert results(conn, restore.precheck_sql()) == [(False, str(drill_id), str(backup_id))]
    ((_, table, rows, snap, owner, acl),) = results(conn, restore.PENDING_SQL)
    assert (table, rows, snap) == (TABLE, 3, checksum) and "=r/" in acl

    make_table(conn, list(reversed(ROWS)))  # the same rows, another order
    assert results(conn, restore.checksum_sql(TABLE)) == [(3, checksum)]
    assert results(conn, restore.acl_sql(TABLE)) == [(owner, acl)]
    conn.execute(f"update {TABLE} set premium = 12.6 where member_id = 'M000000001'")
    assert results(conn, restore.checksum_sql(TABLE))[0][1] != checksum


def test_a_table_something_depends_on_is_not_dropped_and_nothing_is_recorded(conn):
    conn.execute(f"create view gold.current_members as select * from {TABLE}")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        results(conn, restore.drop_sql())
    conn.execute("rollback")
    assert results(conn, f"select to_regclass('{TABLE}') is not null") == [(True,)]
    assert results(conn, "select count(*) from ops.restore_drill") == [(0,)]
