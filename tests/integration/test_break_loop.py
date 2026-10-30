"""Stage 4 (S4.5): every `dip break` scenario through detect, triage, propose, approve, fix
and check (dip/agents/break_loop.py), on recorded model turns: CI calls no model."""

from __future__ import annotations

from datetime import timedelta

import pytest

from dip import BUCKET_LANDING
from dip.agents import approvals, break_loop
from dip.agents.llm import Recording
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows

SCENARIOS = break_loop.load_scenarios()


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_the_scenario_is_detected_triaged_approved_and_fixed(sc, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    result = break_loop.run_scenario(
        sc, DSN, lambda name: Recording(break_loop.RECORDINGS / f"{name}.json"), tmp_path
    )
    assert result.problems == [], result.steps
    if sc.get("timing") == "late":  # S9.4: the SLA check's, no triage
        assert result.actions == [] and result.steps[-1] == "check: loaded, incident closed"
    elif sc["expect"] == "loads":  # S9.4: a tolerated change, no triage
        assert result.actions == [] and result.steps[-1] == "check: loaded, nothing quarantined"
    else:
        assert set(result.actions) <= set(sc["expect"]) | {"replay"}
        assert result.steps[-1] == "check: no open quarantine for the feed"


def test_every_dip_break_scenario_is_in_the_loop():
    from dip.breakages import SCENARIOS as BREAKS

    looped = {step["op"] for sc in SCENARIOS for step in sc["break"]}
    assert looped == set(BREAKS)


def test_a_resend_closes_only_what_an_executed_resend_request_covered(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    contracts = load_contracts((break_loop.CONTRACTS,))
    good = (break_loop.BASE / "elig_base.csv").read_bytes()
    feed = "healthcare.eligibility"

    def land(name: str, data: bytes, hours: int):
        store.put(BUCKET_LANDING, f"{feed}/{name}", data)
        return process_landing(store, warehouse, contracts, break_loop.AT + timedelta(hours=hours))

    broken = good.replace(b"member_id,", b"memberid,", 1)
    land("elig_20261020.csv", broken, 0)
    land("elig_20261021.csv", broken.replace(b"Hallett", b"Hallet"), 0)
    ids = [r[0] for r in rows(warehouse, "select quarantine_id from ops.quarantine order by 1")]
    assert len(ids) == 4  # two files, each missing member_id and with memberid
    # A person asked the vendor to resend the first file only.
    first = [i for i in ids if i <= 2]
    payload = {"group_id": "x", "feed": feed, "quarantine_ids": first, "message": "resend"}
    aid = approvals.create(warehouse.conn, "triage", None, "request_resend", payload)
    approvals.decide(warehouse.conn, aid, True, "human:test")
    approvals.execute(warehouse.conn, store, aid, "human:test")

    [o] = land("elig_20261020_resend.csv", good, 1)  # the resend keeps its date
    assert (o.status, o.superseded) == ("loaded", 2)
    assert rows(
        warehouse,
        "select quarantine_id, status, superseded_by is not null from ops.quarantine order by 1",
    ) == [
        (1, "superseded", True),
        (2, "superseded", True),
        (3, "open", False),
        (4, "open", False),
    ]


def test_the_next_days_routine_file_does_not_close_a_resend_request(warehouse, tmp_path):
    # An incremental feed: yesterday's file was quarantined and a resend requested; today's
    # ordinary file (another business date) loads, but yesterday's data has not arrived.
    store = LocalStore(tmp_path)
    contracts = load_contracts((break_loop.CONTRACTS,))
    good = (break_loop.BASE / "elig_base.csv").read_bytes()
    feed = "healthcare.eligibility"
    store.put(BUCKET_LANDING, f"{feed}/elig_20261020.csv", good.replace(b"member_id,", b"m,", 1))
    process_landing(store, warehouse, contracts, break_loop.AT)
    ids = [r[0] for r in rows(warehouse, "select quarantine_id from ops.quarantine")]
    payload = {"group_id": "x", "feed": feed, "quarantine_ids": ids, "message": "resend"}
    aid = approvals.create(warehouse.conn, "triage", None, "request_resend", payload)
    approvals.decide(warehouse.conn, aid, True, "human:test")
    approvals.execute(warehouse.conn, store, aid, "human:test")
    store.put(BUCKET_LANDING, f"{feed}/elig_20261021.csv", good)
    [o] = process_landing(store, warehouse, contracts, break_loop.AT)
    assert (o.status, o.superseded) == ("loaded", 0)
    assert {r[0] for r in rows(warehouse, "select status from ops.quarantine")} == {"open"}
