"""Stage 6 (S6.1, S6.3): the triage agent's Act tier. The server checks the approval before
anything runs and refuses everything a person did not approve for this agent, audited as
denied; the recorded Act-mode evals replay through the real MCP server (no model call)."""

from __future__ import annotations

import pytest

from dip import BUCKET_LANDING
from dip.agents import act_evals, approvals, evals
from dip.agents.llm import Recording
from dip.agents.registry import Denied, Policy
from dip.agents.tools import build_registry
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows

DOC = act_evals.load()


@pytest.fixture
def world(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, f"{act_evals.FEED}/elig_20261021.csv",
              DOC["file"]["content"].encode())  # fmt: skip
    process_landing(store, warehouse, evals.eval_contracts(), evals.INGEST_AT)
    ids = [i for g in evals.open_groups(warehouse.conn).values() for i in g]
    return warehouse, store, build_registry(warehouse.conn, store, evals.eval_contracts()), ids


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ({"action": "request_resend", "state": "pending"}, "is pending"),
        ({"action": "discard", "state": "rejected"}, "is rejected"),
        ({"action": "request_resend", "state": "expired"}, "expired at"),
        ({"action": "discard", "state": "tampered"}, "payload changed"),
        ({"action": "discard", "state": "agent_approved"}, "not approved by a person"),
        ({"action": "request_resend", "state": "approved", "by": "contract_author"},
         "proposed by contract_author"),
        ({"action": "contract_pr", "state": "approved"}, "may not execute a 'contract_pr'"),
    ],
)  # fmt: skip
def test_the_server_refuses_what_no_person_approved_for_the_agent(world, spec, why):
    wh, _, reg, ids = world
    act_evals._write_approvals(wh.conn, [spec], ids)
    before = rows(wh, "select status, executed_by from ops.approvals")
    with pytest.raises(Denied, match=why):
        reg.call(Policy.load("triage"), "r1", "execute_approved_operation", {"approval_id": 1})
    # Nothing changed, not even an expired row's status, and the refusal is audited as such.
    assert rows(wh, "select status, executed_by from ops.approvals") == before
    assert rows(wh, "select decision, left(reason, 20) is not null from ops.audit_log") == [
        ("denied", True)
    ]


def test_denied_then_allowed(world):
    wh, _, reg, ids = world
    policy = Policy.load("triage")
    act_evals._write_approvals(wh.conn, [{"action": "request_resend", "state": "pending",
                                          "message": "Please resend."}], ids)  # fmt: skip
    with pytest.raises(Denied, match="pending"):
        reg.call(policy, "r1", "execute_approved_operation", {"approval_id": 1})
    approvals.decide(wh.conn, 1, True, "human:test")
    result = reg.call(policy, "r2", "execute_approved_operation", {"approval_id": 1})
    assert result["status"] == "executed"
    assert result["result"]["vendor_request"] == "Please resend."
    assert rows(wh, "select status, executed_by from ops.approvals") == [("executed", "triage")]
    assert rows(wh, "select run_id, decision, result_status from ops.audit_log order by 1") == [
        ("r1", "denied", None), ("r2", "allowed", "ok")
    ]  # fmt: skip
    # Once is all: the executed approval does not run again.
    with pytest.raises(Denied, match="is executed"):
        reg.call(policy, "r3", "execute_approved_operation", {"approval_id": 1})


@pytest.mark.parametrize("case", DOC["cases"], ids=[c["name"] for c in DOC["cases"]])
def test_recorded_act_case(case, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    llm = Recording(act_evals.RECORDINGS / f"{case['name']}.json")
    result = act_evals.run_case(DOC, case, DSN, llm, tmp_path)
    assert result.passed, result
