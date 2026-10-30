"""Stage 9 (S9.1): the agent_chain DAG's task claims open incidents once, runs or skips
the chain on each, and records the outcome in ops.agent_run. The chain itself is
stubbed: what is under test is the claim and the record, not the agents."""

from __future__ import annotations

from datetime import UTC, datetime

from dip.agents import chain_job
from dip.incidents import open_incident
from tests.integration.conftest import rows

MCP = {"DIP_MCP_URL": "http://mcp:8765/mcp", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}


def _open(wh, feed, kind="DAG.FAILED", detail="a task failed"):
    with wh.conn.cursor() as cur:
        open_incident(cur, feed, kind, detail)
    wh.conn.commit()
    return rows(wh, "select max(incident_id) from ops.incident")[0][0]


def test_without_a_model_key_every_incident_is_skipped_and_claimed_once(warehouse):
    a = _open(warehouse, "healthcare.eligibility")
    b = _open(warehouse, "finance.invoices")

    s = chain_job.run(warehouse.conn, env=MCP, dag_run_id="manual__1")
    assert s["claimed"] == 2 and s["skipped"] == [a, b] and s["done"] == [] and s["failed"] == []
    assert rows(
        warehouse,
        "select incident_id, status, dag_run_id, finished_at is not null"
        " from ops.agent_run order by incident_id",
    ) == [(a, "skipped", "manual__1", True), (b, "skipped", "manual__1", True)]
    assert "no model key" in rows(warehouse, "select reason from ops.agent_run limit 1")[0][0]

    # the next run finds nothing: the claim rows are the memory
    assert chain_job.run(warehouse.conn, env=MCP)["claimed"] == 0


def test_a_run_records_the_chain_and_a_failure_stays_on_its_incident(warehouse):
    a = _open(warehouse, "healthcare.eligibility", "FILE.QUARANTINED", "elig_20261021.csv")
    b = _open(warehouse, "finance.invoices")
    env = {**MCP, "GEMINI_API_KEY": "g"}
    calls = []

    def runner(conn, incident_id, env_, provider):
        calls.append((incident_id, provider))
        if incident_id == b:
            raise RuntimeError("mcp unreachable")
        return {
            "chain_id": "chain-abc",
            "diagnosis": {"root_cause": "quarantined_records"},
            "handoff_refused": None,
        }

    s = chain_job.run(warehouse.conn, env=env, runner=runner)
    assert calls == [(a, "gemini"), (b, "gemini")]
    assert s["done"] == [a] and s["failed"] == [b] and s["skipped"] == []
    got = rows(
        warehouse,
        "select incident_id, status, chain_id, root_cause, reason"
        " from ops.agent_run order by incident_id",
    )
    assert got[0] == (a, "done", "chain-abc", "quarantined_records", None)
    assert got[1][:2] == (b, "failed") and got[1][4].startswith("RuntimeError: mcp unreachable")
    # neither is claimed again, including the failed one (a person re-runs it)
    assert chain_job.run(warehouse.conn, env=env, runner=runner)["claimed"] == 0
    assert len(calls) == 2


def test_closed_incidents_are_not_claimed_and_the_limit_holds(warehouse):
    ids = [_open(warehouse, f"finance.feed{i}") for i in range(4)]
    with warehouse.conn.cursor() as cur:
        cur.execute(
            "update ops.incident set closed_at = %s where incident_id = %s",
            (datetime.now(UTC), ids[0]),
        )
    warehouse.conn.commit()
    first = chain_job.claim(warehouse.conn, limit=2)
    assert [i for i, _, _ in first] == ids[1:3]
    assert [(f, k) for _, f, k in first] == [
        ("finance.feed1", "DAG.FAILED"),
        ("finance.feed2", "DAG.FAILED"),
    ]
    second = chain_job.claim(warehouse.conn, limit=2)
    assert [i for i, _, _ in second] == [ids[3]]
    assert chain_job.claim(warehouse.conn, limit=2) == []
