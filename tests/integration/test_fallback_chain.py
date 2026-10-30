"""The free fallback in the incident -> triage chain (Stage 10, S10.1; PLAN.md section 8.12),
through the real MCP server and graph: an overloaded Claude hands each step to Gemini (its
turns replayed from recordings/gemini/), and a step that already wrote is never restarted."""

from __future__ import annotations

import asyncio

import pytest

from dip.agents import graph, incident_evals
from dip.agents.llm import Failover, Message, Recording, StepWrote
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows
from tests.integration.test_incident_chain import MISSING, _chain, _land

GEMINI = incident_evals.RECORDINGS / "gemini"


class Overloaded(Exception):
    status_code = 529


class Claude:
    """Claude, overloaded from the first turn, or after `after` scripted turns."""

    def __init__(self, turns: list[Message] | None = None):
        self.turns = list(turns or [])

    def create(self, **kwargs):
        if not self.turns:
            raise Overloaded("overloaded_error")
        return self.turns.pop(0)


def _gemini_cases() -> list[dict]:
    return [c for c in incident_evals.load_cases()
            if (GEMINI / f"{c['name']}_incident.json").exists()]  # fmt: skip


@pytest.mark.parametrize("case", _gemini_cases(), ids=lambda c: c["name"])
def test_an_overloaded_claude_hands_every_step_to_gemini(case, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    answered: list[str] = []

    def gemini(step: str):
        def make():
            answered.append(step)
            return Recording(GEMINI / f"{case['name']}_{step}.json")

        return make

    result = incident_evals.run_case(
        case,
        DSN,
        lambda step: Failover([("anthropic", Claude), ("gemini", gemini(step))]),
        tmp_path,
    )
    # Gemini answered every step that ran, and the chain is whole in the audit log. The
    # scores are Gemini's own (docs/agents/EVALS.md); here the chain must complete.
    assert answered and answered[0] == "incident"
    assert result.chain_audited and not result.violations, result


def test_a_step_that_wrote_is_not_restarted_on_gemini(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    _land(store, warehouse, "elig_20261021.csv", MISSING)
    (iid,) = rows(warehouse, "select incident_id from ops.incident")[0]
    srv, chain_conn, chain = _chain(warehouse.conn, store, {})
    diagnosis = {"incident_id": iid, "root_cause": "transform_failure", "summary": "s",
                 "evidence": ["e"], "confidence": 0.5, "quarantine_ids": []}  # fmt: skip
    wrote = Message([{"type": "tool_use", "id": "t1", "name": "report_diagnosis",
                      "input": diagnosis}], "tool_use", {}, "scripted")  # fmt: skip

    def never():
        raise AssertionError("the step was restarted on Gemini after it had written")

    chain.llm_for = lambda step: Failover([("anthropic", lambda: Claude([wrote])),
                                           ("gemini", never)])  # fmt: skip
    try:
        with pytest.raises(StepWrote):
            asyncio.run(graph.run_chain(chain, iid, "c-wrote"))
    finally:
        srv.__exit__(None, None, None)
        chain_conn.close()
    # The diagnosis was written once, by the first provider's step.
    assert rows(warehouse, "select count(*) from ops.incident_diagnosis") == [(1,)]
