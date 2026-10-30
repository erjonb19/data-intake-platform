"""Stage 9 (S9.2): the approvals page's routes, as the dip_approver login the deployed page
uses. A person lists, shows and decides proposals and reads the audit log; the page can
decide but never execute, and the approval rules are the CLI's (dip.agents.approvals)."""

from __future__ import annotations

import pytest

from dip.agents import approvals
from dip.contracts import REPO_ROOT
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows

ROLES_SQL = REPO_ROOT / "infra/terraform/files/postgres-roles.sql"
# A fixed, public test password: fine for a throwaway CI database, never for a deployment.
PASSWORD = "ci-approver-0123456789"
HOST = "testserver"  # the Host header TestClient sends
TOKEN = "ci-approvals-token"


def approver_conn():
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(DSN)
    info.update(user="dip_approver", password=PASSWORD)
    return psycopg.connect(**info)


@pytest.fixture
def page(warehouse):
    from fastapi.testclient import TestClient

    from dip.agents.gateway import create_app

    with warehouse.conn.cursor() as cur:
        cur.execute(ROLES_SQL.read_text(encoding="utf-8"))
        cur.execute(f"alter role dip_approver password '{PASSWORD}'")
    warehouse.conn.commit()
    warehouse.init()  # the role exists now: its grants apply
    app = create_app(approver_conn, [HOST], token=TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client


def _propose(wh, action="discard", agent="triage", **extra):
    payload = {"group_id": "healthcare.eligibility|ROW.TYPE", "quarantine_ids": [1, 2],
               "root_cause": "vendor_bad_data", "confidence": 0.9, **extra}  # fmt: skip
    return approvals.create(wh.conn, agent, "run-1", action, payload)


def _decide(page, approval_id, decision, reviewer="alice", **kw):
    headers = {"X-Reviewer": reviewer} if reviewer else {}
    return page.post(f"/api/approvals/{approval_id}/decision", json={"decision": decision, **kw},
                     headers=headers)  # fmt: skip


def test_the_page_and_the_pending_list(page, warehouse):
    a = _propose(warehouse)
    b = _propose(warehouse, action="request_resend", message="please resend")
    assert page.get("/").status_code == 200 and "<title>Approvals</title>" in page.get("/").text
    got = page.get("/api/approvals").json()
    assert [r["approval_id"] for r in got] == [b, a]  # newest first
    assert got[1] | {"created_at": None, "expires_at": None} == {
        "approval_id": a, "created_at": None, "agent_id": "triage", "action": "discard",
        "status": "pending", "expires_at": None, "group_id": "healthcare.eligibility|ROW.TYPE",
        "entries": 2, "root_cause": "vendor_bad_data", "confidence": 0.9,
    }  # fmt: skip
    shown = page.get(f"/api/approvals/{b}").json()
    assert shown["payload"]["message"] == "please resend" and shown["diff"] is None
    assert page.get("/api/approvals/999999").status_code == 404
    assert page.get("/api/approvals?status=bogus").status_code == 422


def test_a_contract_pr_shows_its_diff(page, warehouse):
    a = _propose(warehouse, action="contract_pr", agent="author", diff="--- a\n+++ b\n+x\n")
    assert page.get(f"/api/approvals/{a}").json()["diff"] == "--- a\n+++ b\n+x\n"


def test_approve_records_the_person_and_audits_it(page, warehouse):
    a = _propose(warehouse)
    r = _decide(page, a, "approve", note="looks right")
    assert r.status_code == 200, r.text
    assert r.json() == {"approval_id": a, "status": "approved",
                        "decided_by": "human:alice@dip_approver"}  # fmt: skip
    got = rows(warehouse, "select status, decided_by, decision_note,"
               " approved_sha256 = payload_sha256 from ops.approvals where approval_id = %s",
               (a,))  # fmt: skip
    assert got == [("approved", "human:alice@dip_approver", "looks right", True)]
    audit = page.get("/api/audit", params={"principal": "human:alice@dip_approver"}).json()
    assert [(x["tool"], x["tier"], x["decision"], x["result_status"]) for x in audit] == [
        ("approvals.approve", "act", "allowed", "approved")
    ]


def test_reject_then_execute_is_refused(page, warehouse, tmp_path):
    a = _propose(warehouse)
    assert _decide(page, a, "reject").json()["status"] == "rejected"
    with pytest.raises(approvals.ApprovalError, match="is rejected, not approved"):
        approvals.execute(warehouse.conn, LocalStore(tmp_path), a, "human:bob@warehouse")
    # a second decision is refused, and the refusal is audited
    again = _decide(page, a, "approve")
    assert again.status_code == 409 and "not pending" in again.json()["detail"]
    denied = page.get("/api/audit", params={"decision": "denied"}).json()
    assert [(x["tool"], x["principal"]) for x in denied] == [
        ("approvals.approve", "human:alice@dip_approver")
    ]
    assert rows(warehouse, "select status from ops.approvals where approval_id = %s", (a,)) == [
        ("rejected",)
    ]


def test_a_decision_needs_a_reviewer_a_known_host_and_a_valid_body(page, warehouse):
    a = _propose(warehouse)
    assert _decide(page, a, "approve", reviewer=None).status_code == 400
    assert _decide(page, a, "approve", reviewer="alice<script>").status_code == 400
    assert _decide(page, a, "execute").status_code == 422
    # DNS rebinding: a page on another origin that resolves to the port-forward
    r = page.post(f"/api/approvals/{a}/decision", json={"decision": "approve"},
                  headers={"X-Reviewer": "alice", "Host": "evil.example:8081"})  # fmt: skip
    assert r.status_code == 400
    assert rows(warehouse, "select status from ops.approvals where approval_id = %s", (a,)) == [
        ("pending",)
    ]


def test_the_login_can_decide_but_never_record_an_execution(page, warehouse):
    import psycopg

    a = _propose(warehouse)
    refused = pytest.raises(psycopg.errors.InsufficientPrivilege)
    with approver_conn() as conn, conn.cursor() as cur, refused:
        cur.execute("update ops.approvals set executed_by = 'x' where approval_id = %s", (a,))
    refused = pytest.raises(psycopg.errors.InsufficientPrivilege)
    with approver_conn() as conn, conn.cursor() as cur, refused:
        cur.execute("select * from ops.quarantine")


def test_audit_filters(page, warehouse):
    from dip.agents.registry import Registry

    reg = Registry(warehouse.conn)
    reg.audit("triage", "r1", "list_quarantine", "read", {}, "allowed", None, "ok", 3)
    reg.audit("triage", "r1", "propose_operation", "propose", {}, "denied", "rate", None, 1)
    reg.audit("reader", "r2", "list_quarantine", "read", {}, "allowed", None, "ok", 2)
    got = page.get("/api/audit", params={"tool": "list_quarantine"}).json()
    assert [x["principal"] for x in got] == ["reader", "triage"]
    got = page.get("/api/audit", params={"principal": "triage", "decision": "denied"}).json()
    assert [(x["tool"], x["reason"]) for x in got] == [("propose_operation", "rate")]
    assert page.get("/api/audit", params={"since": "2999-01-01T00:00:00Z"}).json() == []
    assert len(page.get("/api/audit", params={"limit": 1}).json()) == 1


def test_every_api_route_needs_the_token(page, warehouse):
    a = _propose(warehouse)
    for h in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        assert page.get("/api/approvals", headers=h).status_code == 401
        assert page.get("/api/audit", headers=h).status_code == 401
        r = page.post(f"/api/approvals/{a}/decision", json={"decision": "approve"},
                      headers={"X-Reviewer": "mallory", **h})  # fmt: skip
        assert r.status_code == 401
    assert page.get("/", headers={"Authorization": ""}).status_code == 200  # the page itself
    assert rows(warehouse, "select status from ops.approvals where approval_id = %s", (a,)) == [
        ("pending",)
    ]


def test_a_decision_record_cannot_be_rewritten(page, warehouse):
    import psycopg

    a = _propose(warehouse)
    b = _propose(warehouse, action="request_resend")
    assert _decide(page, a, "approve", note="ok").status_code == 200
    assert _decide(page, b, "reject").status_code == 200
    edits = [
        ("update ops.approvals set decision_note = 'edited' where approval_id = %s", a),
        ("update ops.approvals set decided_at = now() where approval_id = %s", a),
        ("update ops.approvals set approved_sha256 = payload_sha256 where approval_id = %s", b),
    ]
    for sql, approval_id in edits:
        refused = pytest.raises(psycopg.errors.RaiseException, match="cannot be rewritten")
        with approver_conn() as conn, conn.cursor() as cur, refused:
            cur.execute(sql, (approval_id,))


def test_a_proposal_from_a_chain_shows_the_provider_it_ran_on(page, warehouse):
    """S10.1: a proposal made on the free fallback is labelled with its provider."""
    from dip.incidents import open_incident

    with warehouse.conn.cursor() as cur:
        iid = open_incident(cur, "healthcare.eligibility", "DAG.FAILED", "a task failed")
        cur.execute("insert into ops.agent_run (incident_id, status, chain_id, provider)"
                    " values (%s, 'done', 'chain-fb', 'incident:anthropic triage:gemini')",
                    (iid,))  # fmt: skip
    warehouse.conn.commit()
    a = approvals.create(warehouse.conn, "triage", "chain-fb/triage", "discard",
                         {"group_id": "g", "quarantine_ids": [1]})  # fmt: skip
    b = _propose(warehouse)  # a person's own `dip triage run`: no chain
    assert page.get(f"/api/approvals/{a}").json()["provider"] == "incident:anthropic triage:gemini"
    assert page.get(f"/api/approvals/{b}").json()["provider"] is None


def test_a_running_chain_without_a_provider_yet_is_labelled_unknown(page, warehouse):
    from dip.incidents import open_incident

    with warehouse.conn.cursor() as cur:
        iid = open_incident(cur, "healthcare.eligibility", "DAG.FAILED", "a task failed")
        cur.execute("insert into ops.agent_run (incident_id, status, chain_id)"
                    " values (%s, 'running', 'chain-run')", (iid,))  # fmt: skip
    warehouse.conn.commit()
    a = approvals.create(warehouse.conn, "triage", "chain-run/triage@gemini", "discard",
                         {"group_id": "g", "quarantine_ids": [1]})  # fmt: skip
    assert page.get(f"/api/approvals/{a}").json()["provider"] == "unknown"


def test_the_audit_log_filters_by_chain(page, warehouse):
    """Stage 10: one intake chain's trail, call by call."""
    from dip.agents.registry import Registry

    reg = Registry(warehouse.conn)
    reg.audit("profile", "intake-x/profile", "report_profile", "propose", {}, "allowed", None,
              "ok", 3)  # fmt: skip
    reg.audit("triage", "chain-y/triage", "propose_operation", "propose", {}, "allowed", None,
              "ok", 3)  # fmt: skip
    got = page.get("/api/audit", params={"chain": "intake-x"}).json()
    assert [r["principal"] for r in got] == ["profile"]
