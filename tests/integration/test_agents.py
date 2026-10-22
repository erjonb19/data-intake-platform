"""Stage 4: the tool registry's policy and audit log, human approvals, the MCP server over
HTTP with per-agent keys, and the triage evals replayed from their recordings (no model
call: CI never calls an LLM)."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING
from dip.agents import approvals, evals, triage
from dip.agents.llm import Recording
from dip.agents.registry import Denied, Policy, human_policy
from dip.agents.tools import build_registry
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

HEADER = "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref"
AT = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def setup(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    no_member_id = (
        HEADER.removeprefix("member_id,") + "\nAlex,Hallett,1980-02-14,F,HMO,2024-01-01,,\n"
    )
    store.put(BUCKET_LANDING, "healthcare.eligibility/elig_missing.csv", no_member_id.encode())
    process_landing(store, warehouse, load_contracts(), AT)
    reg = build_registry(warehouse.conn, store, load_contracts())
    return warehouse, store, reg


def audit(wh):
    return rows(wh, "select principal, tool, tier, decision, reason, result_status"
                    " from ops.audit_log order by audit_id")  # fmt: skip


def test_policy_allows_reads_and_refuses_act_and_unknown_tools(setup):
    wh, _, reg = setup
    policy = Policy.load("triage")
    groups = reg.call(policy, "r1", "list_quarantine_groups", {})["groups"]
    assert [g["reason_code"] for g in groups] == ["SCHEMA.MISSING_COLUMN:member_id"]
    for name, args, why in [
        ("execute_approved_operation", {"approval_id": 1}, "not in triage's policy"),
        ("drop_everything", {}, "unknown tool"),
    ]:
        with pytest.raises(Denied, match=why):
            reg.call(policy, "r1", name, args)
    with pytest.raises(Denied, match="action 'release'"):
        reg.call(policy, "r1", "propose_operation", {"action": "release"})
    log = audit(wh)
    assert [(t, d) for _, t, _, d, _, _ in log] == [
        ("list_quarantine_groups", "allowed"),
        ("execute_approved_operation", "denied"),
        ("drop_everything", "denied"),
        ("propose_operation", "denied"),
    ]
    assert log[1][2] == "act"


def test_call_limit_per_run(setup):
    _, _, reg = setup
    policy = Policy.load("triage")
    policy.limits["max_calls_per_run"] = 2
    reg.call(policy, "r2", "get_feed_history", {"feed": "healthcare.eligibility"})
    reg.call(policy, "r2", "get_feed_history", {"feed": "healthcare.eligibility"})
    with pytest.raises(Denied, match="over 2 calls"):
        reg.call(policy, "r2", "get_feed_history", {"feed": "healthcare.eligibility"})
    reg.call(policy, "r3", "get_feed_history", {"feed": "healthcare.eligibility"})  # a new run


def test_audit_log_is_append_only(setup):
    import psycopg

    wh, _, reg = setup
    reg.call(Policy.load("triage"), "r", "list_quarantine_groups", {})
    for sql in ("update ops.audit_log set decision = 'denied'", "delete from ops.audit_log",
                "truncate ops.audit_log"):  # fmt: skip
        with (
            pytest.raises(psycopg.errors.RaiseException, match="append-only"),
            wh.conn.cursor() as cur,
        ):
            cur.execute(sql)
        wh.conn.rollback()
    assert scalar(wh, "select count(*) from ops.audit_log") == 1


def _propose(reg, action="discard"):
    policy = Policy.load("triage")
    [g] = reg.call(policy, "r", "list_quarantine_groups", {})["groups"]
    out = reg.call(policy, "r", "propose_operation", {
        "action": action, "group_id": g["group_id"], "quarantine_ids": g["quarantine_ids"],
        "root_cause": "schema_drift", "rationale": "test", "confidence": 0.9})  # fmt: skip
    return out["approval_id"], g["quarantine_ids"]


def test_proposal_waits_for_a_person_then_runs_once(setup):
    wh, store, reg = setup
    approval_id, ids = _propose(reg)
    me = human_policy("tester")
    with pytest.raises(approvals.ApprovalError, match="pending, not approved"):
        reg.call(me, None, "execute_approved_operation", {"approval_id": approval_id})
    assert approvals.decide(wh.conn, approval_id, True, me.agent) == "approved"
    assert reg.call(me, None, "execute_approved_operation",
                    {"approval_id": approval_id}) == {"discarded": ids}  # fmt: skip
    assert scalar(wh, "select status from ops.quarantine where quarantine_id = %s",
                  (ids[0],)) == "discarded"  # fmt: skip
    with pytest.raises(approvals.ApprovalError, match="executed, not approved"):
        approvals.execute(wh.conn, store, approval_id, me.agent)


def test_a_payload_changed_after_proposal_never_runs(setup):
    wh, store, reg = setup
    approval_id, _ = _propose(reg)
    approvals.decide(wh.conn, approval_id, True, "human:tester")
    with wh.conn.cursor() as cur:
        cur.execute("update ops.approvals set payload = jsonb_set(payload, '{quarantine_ids}',"
                    " '[1, 2, 3]') where approval_id = %s", (approval_id,))  # fmt: skip
    wh.conn.commit()
    with pytest.raises(approvals.ApprovalError, match="payload changed"):
        approvals.execute(wh.conn, store, approval_id, "human:tester")
    assert scalar(wh, "select status from ops.quarantine order by 1 limit 1") == "open"


def test_an_expired_approval_never_runs(setup):
    wh, store, reg = setup
    approval_id, _ = _propose(reg)
    approvals.decide(wh.conn, approval_id, True, "human:tester")
    with wh.conn.cursor() as cur:
        cur.execute("update ops.approvals set expires_at = now() - interval '1 minute'"
                    " where approval_id = %s", (approval_id,))  # fmt: skip
    wh.conn.commit()
    with pytest.raises(approvals.ApprovalError, match="expired"):
        approvals.execute(wh.conn, store, approval_id, "human:tester")
    assert scalar(wh, "select status from ops.approvals") == "expired"


def test_replay_reprocesses_the_raw_copy_under_the_current_contract(warehouse, tmp_path):
    """Quarantined under an older contract that lacked EPO; approved replay lands the raw
    copy again and the current contract loads it."""
    case = next(c for c in evals.load_cases() if c["name"] == "replay_after_contract_fix")
    evals.build_state(warehouse.conn, tmp_path, case)
    store = LocalStore(tmp_path)
    reg = build_registry(warehouse.conn, store, load_contracts())
    policy = Policy.load("triage")
    g = next(g for g in reg.call(policy, "r", "list_quarantine_groups", {})["groups"]
             if g["reason_code"] == "FILE.BAD_ROW_RATIO")  # fmt: skip
    approval_id = reg.call(policy, "r", "propose_operation", {
        "action": "replay", "group_id": g["group_id"], "quarantine_ids": g["quarantine_ids"],
        "root_cause": "invalid_values", "rationale": "contract now allows EPO",
        "confidence": 0.8})["approval_id"]  # fmt: skip
    approvals.decide(warehouse.conn, approval_id, True, "human:tester")
    result = approvals.execute(warehouse.conn, store, approval_id, "human:tester")
    assert result == {"relanded": ["healthcare.eligibility/elig_epo.csv"]}
    [o] = process_landing(store, warehouse, load_contracts(), AT)
    assert (o.status, o.accepted) == ("loaded", 3)
    assert scalar(warehouse, "select count(*) from ops.quarantine where status = 'open'") == 0


def test_mcp_server_requires_a_key_and_enforces_the_policy(setup, tmp_path):
    import httpx2

    wh, store, _ = setup
    with evals.LocalServer(wh.conn, store) as srv:
        for headers in ({}, {"Authorization": "Bearer not-a-key"}):
            r = httpx2.post(srv.url, headers=headers, json={"jsonrpc": "2.0", "id": 1,
                                                            "method": "tools/list"})  # fmt: skip
            assert r.status_code == 401

        async def go():
            async with triage.mcp_client(srv.url, srv.key) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                denied = await client.call_tool("execute_approved_operation", {"approval_id": 1})
                ok = await client.call_tool("list_quarantine_groups", {})
                return tools, denied, ok

        tools, denied, ok = asyncio.run(go())
    assert "execute_approved_operation" in tools  # visible, so an attempt is measurable
    assert denied.is_error and "denied by policy" in denied.content[0].text
    assert not ok.is_error and "SCHEMA.MISSING_COLUMN:member_id" in ok.content[0].text
    assert ("triage", "execute_approved_operation", "act", "denied") in [
        (p, t, tier, d) for p, t, tier, d, _, _ in audit(wh)
    ]


ALL_CASES = [(evals.CASES, c) for c in evals.load_cases()] + [
    (evals.CASES.parent / "holdout.yaml", c)
    for c in evals.load_cases(evals.CASES.parent / "holdout.yaml")
]


@pytest.mark.parametrize(("source", "case"), ALL_CASES, ids=[c["name"] for _, c in ALL_CASES])
def test_recorded_triage_eval(warehouse, tmp_path, source, case):
    """The recorded model turns replayed through the real agent loop, MCP server, policy and
    scoring. Fails if a recording no longer matches (re-record with --record)."""
    if not any(w in warehouse.conn.info.dbname for w in ("eval", "test")):
        pytest.skip("needs a throwaway database")
    # A short work directory: Windows keeps paths under 260 characters.
    work = Path(tempfile.mkdtemp(prefix="ev"))
    result = evals.run_case(case, DSN, Recording(evals.RECORDINGS / f"{case['name']}.json"), work)
    assert not result.violations, result.violations
    assert result.passed, [g for g in result.groups if not (g.cause_ok and g.action_ok)]
