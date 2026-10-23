"""Stage 7: incidents raised by breakages, the incident agent's tools, and the LangGraph
incident -> triage chain. The recorded evals replay through the real MCP server and graph
(no model call); the routing and the typed handoff are tested with scripted model turns."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from dip import BUCKET_LANDING
from dip.agents import approvals, evals, graph, incident_evals, triage
from dip.agents.llm import Message, Recording
from dip.incidents import dag_callback
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows

CASES = incident_evals.load_cases()
FEED = "healthcare.eligibility"
MISSING = (
    "first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref\n"
    "Alex,Hallett,1980-02-14,F,HMO,2024-01-01,,\n"
)
GOOD = (
    "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref\n"
    "M000000001,Alex,Hallett,1980-02-14,F,HMO,2024-01-01,,\n"
)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_recorded_chain(case, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    result = incident_evals.run_case(
        case,
        DSN,
        lambda step: Recording(incident_evals.RECORDINGS / f"{case['name']}_{step}.json"),
        tmp_path,
    )
    assert result.passed, result


def _land(store, wh, name: str, text: str, hours: int = 0):
    store.put(BUCKET_LANDING, f"{FEED}/{name}", text.encode())
    return process_landing(
        store, wh, evals.eval_contracts(), evals.INGEST_AT + timedelta(hours=hours)
    )


def test_a_quarantined_file_opens_an_incident_and_its_resend_closes_it(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    [o] = _land(store, warehouse, "elig_20261021.csv", MISSING)
    assert o.status == "quarantined"
    [(iid, kind, still_open)] = rows(
        warehouse, "select incident_id, kind, closed_at is null from ops.incident"
    )
    assert (kind, still_open) == ("FILE.QUARANTINED", True)
    ids = [r[0] for r in rows(warehouse, "select quarantine_id from ops.quarantine")]
    payload = {"group_id": f"{FEED}|x", "feed": FEED, "quarantine_ids": ids, "message": "m"}
    aid = approvals.create(warehouse.conn, "triage", None, "request_resend", payload)
    approvals.decide(warehouse.conn, aid, True, "human:test")
    approvals.execute(warehouse.conn, store, aid, "human:test")
    [o] = _land(store, warehouse, "elig_20261021_resend.csv", GOOD, 1)
    assert (o.status, o.superseded) == ("loaded", 1)
    assert rows(warehouse, "select closed_at is not null from ops.incident") == [(True,)]


def test_a_failed_run_opens_dag_failed_and_the_next_success_closes_it(warehouse):
    run = SimpleNamespace(run_id="manual__1", dag_id="intake__healthcare__eligibility")

    def connect():
        import psycopg

        return psycopg.connect(DSN)

    dag_callback(FEED, True, {"dag_run": run, "reason": "task_failure"}, connect)
    dag_callback(FEED, True, {"dag_run": run}, connect)  # once per day
    assert rows(warehouse, "select kind, detail, closed_at is null from ops.incident") == [
        ("DAG.FAILED", "intake__healthcare__eligibility run manual__1: task_failure", True)
    ]
    dag_callback(FEED, False, {"dag_run": run}, connect)
    assert rows(warehouse, "select closed_at is not null from ops.incident") == [(True,)]


class Scripted:
    """Model turns written by hand: one report_diagnosis call, then a final answer."""

    def __init__(self, diagnosis: dict):
        self.diagnosis, self.turn = diagnosis, 0

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            use = {"type": "tool_use", "id": "t1", "name": "report_diagnosis",
                   "input": self.diagnosis}  # fmt: skip
            return Message([use], "tool_use", {}, "scripted")
        return Message([{"type": "text", "text": "done"}], "end_turn", {}, "scripted")


def _chain(conn, store, diagnosis):
    import psycopg

    srv = evals.LocalServer(conn, store, agents=("incident", "triage"))
    srv.__enter__()
    chain_conn = psycopg.connect(DSN)

    class NoTriage:  # the triage step must not run in these tests
        def create(self, **kwargs):
            raise AssertionError("triage ran")

    chain = graph.Chain(
        conn=chain_conn,
        incident_client=lambda: triage.mcp_client(srv.url, srv.keys["incident"]),
        triage_client=lambda: triage.mcp_client(srv.url, srv.keys["triage"]),
        llm_for=lambda step: Scripted(diagnosis) if step == "incident" else NoTriage(),
    )
    return srv, chain_conn, chain


def test_code_not_the_model_decides_the_handoff(warehouse, tmp_path):
    """A diagnosis other than quarantined records never reaches triage, and one that is
    quarantined records on a closed incident is refused and audited as denied."""
    store = LocalStore(tmp_path)
    _land(store, warehouse, "elig_20261021.csv", MISSING)
    (iid,) = rows(warehouse, "select incident_id from ops.incident")[0]
    for cause, chain_id in (("transform_failure", "c-transform"), ("late_delivery", "c-late")):
        srv, chain_conn, chain = _chain(warehouse.conn, store, {
            "incident_id": iid, "root_cause": cause, "summary": "s", "evidence": ["e"],
            "confidence": 0.5, "quarantine_ids": [1]})  # fmt: skip
        try:
            state = asyncio.run(graph.run_chain(chain, iid, chain_id))
        finally:
            srv.__exit__(None, None, None)
            chain_conn.close()
        assert state["handoff"] is None and state["triage_run"] is None
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.incident set closed_at = now()")
    warehouse.conn.commit()
    srv, chain_conn, chain = _chain(warehouse.conn, store, {
        "incident_id": iid, "root_cause": "quarantined_records", "summary": "s",
        "evidence": ["e"], "confidence": 0.9, "quarantine_ids": [1]})  # fmt: skip
    try:
        state = asyncio.run(graph.run_chain(chain, iid, "c-closed"))
    finally:
        srv.__exit__(None, None, None)
        chain_conn.close()
    assert state["handoff"] is None and "is closed" in state["handoff_refused"]
    assert rows(warehouse, "select decision, reason from ops.audit_log"
                           " where tool = 'handoff_to_triage'") == [
        ("denied", f"incident {iid} is closed")
    ]  # fmt: skip


def test_the_handoff_scope_is_the_incidents_own_quarantine(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    _land(store, warehouse, "elig_20261021.csv", MISSING)
    (iid,) = rows(warehouse, "select incident_id from ops.incident")[0]
    ids = [r[0] for r in rows(warehouse, "select quarantine_id from ops.quarantine")]
    diagnosis = {"incident_id": iid, "summary": "x" * 900, "quarantine_ids": [999, *ids]}
    h, refused = graph.build_handoff(warehouse.conn, iid, diagnosis)
    assert refused is None
    assert h.quarantine_ids == tuple(ids)
    assert h.group_ids == (f"{FEED}|SCHEMA.MISSING_COLUMN:member_id",)
    assert len(h.hypothesis) < 600 and "dropped: [999]" in h.hypothesis
    _, refused = graph.build_handoff(warehouse.conn, iid, {**diagnosis, "incident_id": iid + 1})
    assert refused == "the diagnosis is for another incident"
