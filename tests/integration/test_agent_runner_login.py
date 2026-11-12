"""24.9: the scheduled agent runners work as dip_agent_runner, and that login can do
nothing else.

Each test runs the runners' real SQL (claim, finish, versions, requeue, the handoff's
reads, the audit and model-call inserts, the monthly spend meter) as the role, by
`SET ROLE` on the test connection, so a grant missing from migration 0003 fails here and
not first in the cluster with a model key."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from dip.agents import chain_job, graph, intake_runner, model_calls
from dip.agents.registry import Registry
from dip.incidents import open_incident
from dip.intake_jobs import queue
from tests.integration.conftest import DSN, rows

ROLE = "dip_agent_runner"
MCP = {"DIP_MCP_URL": "u", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}


@pytest.fixture
def runner_conn(warehouse):
    """The warehouse fixture's connection, acting as the agent runners' role."""
    with warehouse.conn.cursor() as cur:
        cur.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}')"
            f" THEN CREATE ROLE {ROLE} NOLOGIN; END IF; END $$"
        )
        cur.execute("select ops.grant_agent_runner()")
    warehouse.conn.commit()
    yield warehouse
    warehouse.conn.rollback()
    with warehouse.conn.cursor() as cur:
        cur.execute("reset role")
    warehouse.conn.commit()


def as_runner(wh):
    with wh.conn.cursor() as cur:
        cur.execute(f"set role {ROLE}")
    wh.conn.commit()


def test_the_chain_claims_finishes_and_records_versions_as_the_runner(runner_conn):
    wh = runner_conn
    with wh.conn.cursor() as cur:
        open_incident(cur, "healthcare.eligibility", "DAG.FAILED", "a task failed")
    wh.conn.commit()
    as_runner(wh)

    def runner(conn, incident_id, env_, providers):
        return {"chain_id": "chain-r", "diagnosis": {"root_cause": "quarantined_records"},
                "providers": {"incident": "gemini"}, "versions": {"incident": "abc"}}  # fmt: skip

    s = chain_job.run(wh.conn, env={**MCP, "GEMINI_API_KEY": "g"}, runner=runner)
    assert len(s["done"]) == 1 and s["failed"] == []
    assert rows(wh, "select status, chain_id from ops.agent_run") == [("done", "chain-r")]


def test_the_intake_team_claims_and_finishes_as_the_runner(runner_conn):
    wh = runner_conn
    with wh.conn.cursor() as cur:
        jid = queue(cur, "finance.a", "onboard", "a" * 64, "landing")
    wh.conn.commit()
    as_runner(wh)
    keys = {v: "k" for v in ("DIP_MCP_URL", *intake_runner.KEYS.values())}
    out = intake_runner.run(
        wh.conn,
        env={**keys, "GEMINI_API_KEY": "g"},
        runner=lambda *a: {
            "outcome": "no_change",
            "chain_id": "intake-a",
            "versions": {"map": "x"},
        },  # fmt: skip
    )
    assert out["claimed"] == 1
    assert rows(wh, "select status from ops.intake_job where job_id = %s", (jid,)) != [("queued",)]


def test_the_handoff_reads_and_the_audit_insert_work_as_the_runner(runner_conn):
    wh = runner_conn
    with wh.conn.cursor() as cur:
        open_incident(cur, "healthcare.eligibility", "FILE.QUARANTINED", "elig.csv")
    wh.conn.commit()
    incident = rows(wh, "select max(incident_id) from ops.incident")[0][0]
    as_runner(wh)
    graph.build_handoff(wh.conn, incident, {"root_cause": "quarantined_records"})
    assert graph.wrote(wh.conn, "chain-x/triage") is False
    assert graph._diagnosis(wh.conn, "chain-x/incident") is None
    Registry(wh.conn).audit("incident", "chain-x/handoff", "handoff", "read", {}, "allowed",
                            None, "ok", 1)  # fmt: skip


def test_model_calls_and_the_month_meter_work_as_the_runner(runner_conn):
    dsn = make_conninfo(DSN, options=f"-c role={ROLE}")
    sink = model_calls.WarehouseSink(dsn)
    now = datetime.now(UTC)
    row = dict.fromkeys(model_calls.COLUMNS)
    row.update(provider="anthropic", source="live", prompt_sha256="x", turn=1, status="ok",
               input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0,
               cost_usd=0.25, started_at=now, finished_at=now, latency_ms=5)  # fmt: skip
    try:
        sink(row)
        assert sink.month_live_usd() == pytest.approx(0.25)
    finally:
        sink.close()


@pytest.mark.parametrize(
    "sql",
    [
        "select * from bronze.csv_record limit 1",
        "select key from ops_secret.deid_key",
        "insert into ops.approvals default values",
        "update ops.incident set closed_at = now()",
        "delete from ops.audit_log",
        "update ops.audit_log set reason = 'x'",
    ],
)
def test_the_runner_cannot_touch_anything_else(runner_conn, sql):
    as_runner(runner_conn)
    with runner_conn.conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(sql)
