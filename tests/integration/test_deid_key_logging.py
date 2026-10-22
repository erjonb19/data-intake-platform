"""The de-identification key never reaches Postgres statement logs or pg_stat_activity.

With `client_min_messages = log`, the server sends this session's own log lines (the ones
`log_statement = all` and `log_min_duration_statement = 0` write, bind parameters
included) back to the client as notices, so the test can read exactly what would be logged.
"""

from __future__ import annotations

import io

import pytest

from dip import deid
from tests.integration.conftest import DSN

KEY = "log-probe-key-" + "7f3a9c" * 8


def test_key_is_not_in_statement_logs_or_pg_stat_activity(warehouse):
    import psycopg

    conn = warehouse.conn
    with conn.cursor() as cur:
        cur.execute("select rolsuper from pg_roles where rolname = current_user")
        if not cur.fetchone()[0]:
            pytest.skip("setting log_statement needs a superuser test database")
        for setting in (
            "log_statement = 'all'",
            "log_min_duration_statement = 0",
            "log_parameter_max_length = -1",
            "log_min_error_statement = 'debug5'",
            "client_min_messages = 'log'",
        ):
            cur.execute(f"set {setting}")
    conn.commit()
    logged: list[str] = []
    conn.add_notice_handler(
        lambda d: logged.append(f"{d.message_primary or ''} {d.message_detail or ''}")
    )

    deid.install_key(conn, KEY)

    assert any("deid_key" in line for line in logged), "statement logging was not captured"
    assert not [line for line in logged if KEY in line]
    with conn.cursor() as cur:
        cur.execute("select key from ops_secret.deid_key where key_id = 'default'")
        assert cur.fetchone()[0] == KEY
    conn.commit()
    with psycopg.connect(DSN) as other, other.cursor() as cur:
        cur.execute("select count(*) from pg_stat_activity where query like %s", (f"%{KEY}%",))
        assert cur.fetchone()[0] == 0


def test_key_with_copy_special_characters_round_trips(warehouse):
    key = "tab\there\\backslash\nnewline\rreturn" + "x" * 32
    deid.install_key(warehouse.conn, key)
    with warehouse.conn.cursor() as cur:
        cur.execute("select key from ops_secret.deid_key where key_id = 'default'")
        assert cur.fetchone()[0] == key
    warehouse.conn.commit()


class _Psycopg2Cursor:
    """Records what a psycopg2 cursor (Airflow's PostgresHook) would send."""

    def __init__(self):
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self.copied = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params or ())

    def copy_expert(self, sql, file: io.StringIO):
        self.statements.append(sql)
        self.copied = file.read()


class _Psycopg2Conn:
    def __init__(self):
        self.cur = _Psycopg2Cursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


def test_psycopg2_path_sends_the_key_only_as_copy_data():
    conn = _Psycopg2Conn()
    deid.install_key(conn, "a\tb\\c\n" + KEY)
    sent = " ".join(conn.cur.statements) + repr(conn.cur.params)
    assert KEY not in sent
    assert conn.cur.copied == "a\\tb\\\\c\\n" + KEY + "\n"
