"""Stage 11 (S11.5): the feedback loop. A rejection records why (fixed list, frozen with the
decision), ops.agent_feedback counts decisions per agent, action and week, and a rejected
triage proposal becomes a candidate eval case whose every value is synthetic; a proposal
over non-synthetic inputs is refused."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
import yaml

from dip import BUCKET_LANDING
from dip.agents import approvals, evals
from dip.agents.feedback import FeedbackError, from_rejection
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import rows

FEED = "healthcare.eligibility"
BAD = (
    "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref\n"
    "M000000011,Robin,Achterberg,1984-05-09,F,HMO,2024-01-01,,\n"
    "M000000012,Dana,Whitfield,1979-08-17,X,PPO,2023-06-01,,\n"
    "M000000013,Morgan,Castellanos,1991-02-28,Q,EPO,2025-01-01,,\n"
)


@pytest.fixture
def rejected(warehouse, tmp_path):
    """A triage proposal for a quarantined group, rejected by a person."""
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, f"{FEED}/elig_20261020.csv", BAD.encode())
    process_landing(store, warehouse, load_contracts(), datetime(2026, 10, 21, 12, tzinfo=UTC))
    sql = (
        "select feed || '|' || reason_code, array_agg(quarantine_id) from ops.quarantine"
        " where reason_code = 'VALUE.NOT_ALLOWED:sex' group by 1"
    )
    [(group, ids)] = rows(warehouse, sql)
    payload = {"group_id": group, "quarantine_ids": ids, "root_cause": "invalid_values",
               "rationale": "two members have a sex code the contract does not allow",
               "confidence": 0.8, "message": "please resend"}  # fmt: skip
    aid = approvals.create(warehouse.conn, "triage", "chain-f/triage", "discard", payload)
    approvals.decide(warehouse.conn, aid, False, "human:alice", "resend, do not discard",
                     "wrong_action")  # fmt: skip
    return warehouse, store, aid


def test_a_rejection_records_why_and_it_is_frozen(rejected):
    wh, _, aid = rejected
    assert rows(wh, "select status, decision_reason from ops.approvals where approval_id = %s",
                (aid,)) == [("rejected", "wrong_action")]  # fmt: skip
    with pytest.raises(psycopg.errors.RaiseException, match="cannot be rewritten"), \
            wh.conn.cursor() as cur:  # fmt: skip
        cur.execute("update ops.approvals set decision_reason = 'other' where approval_id = %s",
                    (aid,))  # fmt: skip
    wh.conn.rollback()
    other = approvals.create(wh.conn, "triage", "r", "request_resend", {"group_id": "g2"})
    with pytest.raises(approvals.ApprovalError, match="for a rejection"):
        approvals.decide(wh.conn, other, True, "human:a", reason="other")
    with pytest.raises(approvals.ApprovalError, match="one of"):
        approvals.decide(wh.conn, other, False, "human:a", reason="because")


def test_the_feedback_view_counts_decisions_per_agent_and_week(rejected):
    wh, _, aid = rejected
    ok = approvals.create(wh.conn, "triage", "r", "request_resend", {"group_id": "g2"})
    approvals.decide(wh.conn, ok, True, "human:a")
    approvals.create(wh.conn, "triage", "r", "replay", {"group_id": "g3"})  # pending: not yet
    got = rows(wh, "select agent, action, approved, rejected, wrong_action from ops.agent_feedback"
                   " order by action")  # fmt: skip
    assert got == [("triage", "discard", 0, 1, 1), ("triage", "request_resend", 1, 0, 0)]


def test_a_rejected_triage_proposal_becomes_a_scrubbed_case(rejected, tmp_path):
    wh, store, aid = rejected
    path = from_rejection(wh.conn, store, load_contracts(), aid, tmp_path / "candidates")
    text = path.read_text("utf-8")
    [case] = yaml.safe_load(text)["cases"]
    assert case["feed"] == FEED and list(case["gold"]) == ["VALUE.NOT_ALLOWED:sex"]
    gold = case["gold"]["VALUE.NOT_ALLOWED:sex"]
    assert gold["root_cause"] == "invalid_values" and "discard" not in gold["acceptable"]
    assert case["from_rejection"]["reason"] == "wrong_action"
    [content] = case["files"].values()
    assert content.splitlines()[0] == BAD.splitlines()[0]  # the header: the schema
    allowed = {"F", "M", "U", "HMO", "PPO", "EPO", "HDHP"}
    originals = {v for line in BAD.splitlines()[1:] for v in line.split(",")
                 if any(c.isalnum() for c in v)} - allowed  # fmt: skip
    cells = {v for line in content.splitlines()[1:] for v in line.split(",")}
    leaked = originals & cells  # cell by cell: a one-letter value is in any text
    assert not leaked, leaked
    assert not {v for v in originals if len(v) > 3 and v in text}  # nor anywhere else
    # it loads as the triage harness reads cases
    assert evals.load_cases(path)[0]["name"] == case["name"]


def test_non_synthetic_inputs_are_refused(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    for group, why in (("mimic.labevents|VALUE.TYPE:x", "synthetic pack"),
                       ("healthcare.nothing|X", "no contract")):  # fmt: skip
        aid = approvals.create(warehouse.conn, "triage", "r", "discard",
                               {"group_id": group, "quarantine_ids": [1]})  # fmt: skip
        approvals.decide(warehouse.conn, aid, False, "human:a", reason="other")
        with pytest.raises(FeedbackError, match=why):
            from_rejection(warehouse.conn, store, load_contracts(), aid, tmp_path)
    pending = approvals.create(warehouse.conn, "triage", "r", "discard", {"group_id": "g"})
    with pytest.raises(FeedbackError, match="not rejected"):
        from_rejection(warehouse.conn, store, load_contracts(), pending, tmp_path)
