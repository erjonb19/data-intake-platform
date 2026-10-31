"""Stage 11 (S11.2): a chain's trace. A recorded incident -> triage chain, replayed with the
model-call sink on, gives every model turn and every audited tool call under its chain id,
in time order; `dip trace` and the approvals page's /api/trace show it."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from dip.agents import incident_evals, model_calls, trace
from dip.agents.llm import Recording
from tests.integration.conftest import DSN

CASE = "schema_drift_hands_off"  # incident, the handoff, then triage


def replay(step: str) -> Recording:
    return Recording(incident_evals.RECORDINGS / f"{CASE}_{step}.json")


@pytest.fixture
def chain():
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    import psycopg

    case = {c["name"]: c for c in incident_evals.load_cases()}[CASE]
    work = Path(tempfile.mkdtemp(prefix="tr"))
    try:
        with model_calls.to_warehouse(DSN):
            result = incident_evals.run_case(case, DSN, replay, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert result.passed
    conn = psycopg.connect(DSN)
    yield conn, f"eval-{CASE}"
    conn.close()


def test_every_model_turn_and_tool_call_is_a_span_in_order(chain):
    conn, chain_id = chain
    spans = trace.spans(conn, chain_id)
    turns = sum(len(Recording(incident_evals.RECORDINGS / f"{CASE}_{s}.json").messages)
                for s in ("incident", "triage"))  # fmt: skip
    with conn.cursor() as cur:
        cur.execute("select count(*) from ops.audit_log where chain_id = %s", (chain_id,))
        (audited,) = cur.fetchone()
    assert sum(s["kind"] == "model" for s in spans) == turns
    assert sum(s["kind"] == "tool" for s in spans) == audited > 0
    assert [s["started_at"] for s in spans] == sorted(s["started_at"] for s in spans)
    assert spans[0]["kind"] == "model"  # the agent asks the model before it calls a tool
    steps = [t["step"] for t in trace.totals(spans)]
    assert steps[0] == "incident" and "triage" in steps
    # each step's model calls come before its first tool call ends, and the steps follow
    # one another: all of incident's spans before triage's model calls
    first_triage = next(i for i, s in enumerate(spans) if s["kind"] == "model"
                        and s["step"] == "triage")  # fmt: skip
    assert all(s["step"] != "triage" or s["kind"] == "tool" for s in spans[:first_triage])
    text = trace.render(chain_id, spans)
    assert f"chain {chain_id}: {len(spans)} spans" in text and "incident:" in text


def test_the_totals_add_up(chain):
    conn, chain_id = chain
    spans = trace.spans(conn, chain_id)
    totals = trace.totals(spans)
    assert sum(t["model_calls"] + t["tool_calls"] for t in totals) == len(spans)
    assert sum(t["cost_usd"] for t in totals) == pytest.approx(
        sum(s["cost_usd"] or 0 for s in spans), abs=1e-6)  # fmt: skip
    assert all(t["wall_ms"] >= 0 for t in totals)


def test_an_unknown_chain_has_no_spans(chain):
    conn, _ = chain
    assert trace.spans(conn, "no-such-chain") == []
    assert trace.render("no-such-chain", []) == "no spans for chain 'no-such-chain'"
