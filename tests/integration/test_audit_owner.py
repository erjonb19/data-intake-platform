"""24.14: the login every Airflow pod holds, `warehouse`, can append to the audit log and
the model-call log but cannot rewrite them.

The warehouse is built the way the cluster builds it: the schemas belong to `warehouse`,
and the migrations run as `warehouse`, as in the warehouse-init Job. Then
postgres-audit-owner.sql runs as the superuser, as the postgres-audit-owner Job does, and
everything else here runs as `warehouse` with `SET ROLE`. The test connection is the
superuser."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql

from dip.migrations import reapply_grants
from dip.warehouse import Warehouse
from tests.integration.conftest import DSN, OWNED_SCHEMAS

AUDIT_OWNER_SQL = (
    Path(__file__).resolve().parents[2] / "infra/terraform/files/postgres-audit-owner.sql"
)
LOGINS = ("dip_mcp", "dip_approver", "dip_agent_runner", "dip_metrics")
LOGS = ("ops.audit_log", "ops.model_call")
AUDIT_ROW = ("insert into ops.audit_log (principal, tool, tier, args_sha256, decision)"
             " values ('t', 'tool', 'read', 'x', 'allowed')")  # fmt: skip
MODEL_ROW = ("insert into ops.model_call (provider, source, prompt_sha256, turn, status,"
             " started_at, finished_at, latency_ms)"
             " values ('gemini', 'recorded', 'x', 1, 'ok', now(), now(), 1)")  # fmt: skip


def run(conn, statement):
    with conn.cursor() as cur:
        cur.execute(statement)
    conn.commit()


def ensure_role(conn, name):
    run(conn, f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}')"
              f" THEN CREATE ROLE {name} NOLOGIN; END IF; END $$")  # fmt: skip


@pytest.fixture
def conn():
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    c = psycopg.connect(DSN)
    for schema in OWNED_SCHEMAS:
        run(c, f"drop schema if exists {schema} cascade")
    for role in ("warehouse", *LOGINS):
        ensure_role(c, role)
    with c.cursor() as cur:
        cur.execute("select current_database()")
        db = cur.fetchone()[0]
    run(c, sql.SQL("grant create on database {} to warehouse").format(sql.Identifier(db)))
    run(c, "create extension if not exists pgcrypto")
    for schema in ("bronze", "silver", "gold", "ops"):  # as postgres-init.sh creates them
        run(c, f"create schema {schema} authorization warehouse")
    run(c, "set role warehouse")
    Warehouse(c).init()
    run(c, "reset role")
    for _ in range(2):  # idempotent: the Job runs on every apply
        run(c, AUDIT_OWNER_SQL.read_text(encoding="utf-8"))
    run(c, "set role warehouse")
    yield c
    c.rollback()
    run(c, "reset role")
    c.close()


def refused(conn, statement):
    with pytest.raises(errors.InsufficientPrivilege), conn.cursor() as cur:
        cur.execute(statement)
    conn.rollback()


def scalar(conn, statement, params=None):
    with conn.cursor() as cur:
        cur.execute(statement, params)
        value = cur.fetchone()[0]
    conn.commit()
    return value


def test_the_logs_and_the_ops_schema_belong_to_a_role_nobody_logs_in_as(conn):
    for name in LOGS:
        assert scalar(conn, "select pg_get_userbyid(relowner) from pg_class"
                            " where oid = %s::regclass", (name,)) == "dip_audit_owner"  # fmt: skip
    assert scalar(conn, "select pg_get_userbyid(nspowner) from pg_namespace"
                        " where nspname = 'ops'") == "dip_audit_owner"  # fmt: skip
    assert scalar(conn, "select rolcanlogin from pg_roles"
                        " where rolname = 'dip_audit_owner'") is False  # fmt: skip


def test_warehouse_appends_and_reads(conn):
    run(conn, AUDIT_ROW)
    run(conn, MODEL_ROW)
    assert scalar(conn, "select count(*) from ops.audit_log") == 1
    assert scalar(conn, "select count(*) from ops.model_call") == 1
    assert scalar(conn, "select count(*) from ops.trace_span") >= 0  # the view still reads them


@pytest.mark.parametrize("log", LOGS)
def test_warehouse_cannot_rewrite_disarm_or_drop_a_log(conn, log):
    run(conn, AUDIT_ROW if log == "ops.audit_log" else MODEL_ROW)
    trigger = "audit_log_append_only" if log == "ops.audit_log" else "model_call_append_only"
    for statement in (
        f"update {log} set at = now()" if log == "ops.audit_log" else f"update {log} set turn = 2",
        f"delete from {log}",
        f"truncate {log}",
        f"alter table {log} disable trigger {trigger}",
        f"alter table {log} add column note text",
        f"drop table {log}",
        f"create or replace function ops.{trigger}() returns trigger"
        " language plpgsql as $$ begin return new; end $$",
    ):
        refused(conn, statement)
    refused(conn, "drop schema ops cascade")


def test_migrations_still_add_tables_to_ops_and_grants_still_apply(conn):
    run(conn, "create table ops.migration_probe (x int)")
    assert scalar(conn, "select pg_get_userbyid(relowner) from pg_class"
                        " where oid = 'ops.migration_probe'::regclass") == "warehouse"  # fmt: skip
    run(conn, "drop table ops.migration_probe")
    assert "grant_mcp_reader" in reapply_grants(conn)  # as the warehouse-init Job runs it
    for login in ("dip_mcp", "dip_approver", "dip_agent_runner"):
        assert scalar(conn, "select has_table_privilege(%s, 'ops.audit_log', 'INSERT')", (login,))
    # A grant option passes on only what warehouse holds: never UPDATE or DELETE.
    with conn.cursor() as cur:
        cur.execute("grant update, delete on ops.audit_log to dip_mcp")  # a warning, no grant
    conn.commit()
    for login in ("warehouse", *LOGINS):
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            for log in LOGS:
                assert not scalar(conn, "select has_table_privilege(%s, %s, %s)",
                                  (login, log, privilege)), (login, log, privilege)  # fmt: skip
