"""24.8: the kill switch for the agents' write tools (DIP_AGENTS_WRITE_DISABLED).

With it set, the registry refuses every propose and act call by an agent (the refusal is
audited as denied by Registry._call, like any other:
tests/integration/test_kill_switch_audit.py), read tools and people are unaffected, and
the scheduled runners claim nothing."""

from __future__ import annotations

import pytest

from dip.agents import chain_job, intake_runner
from dip.agents.registry import Policy, Registry, Tool, human_policy, writes_disabled

AGENT = Policy("triage", "act", frozenset({"*"}))


def registry() -> Registry:
    r = Registry(conn=None)
    for name, tier in (("read_x", "read"), ("propose_x", "propose"), ("act_x", "act")):
        r.register(Tool(name, tier, "", {"type": "object", "properties": {}}, lambda: None))
    return r


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_the_switch_reads_truthy_values(value):
    assert writes_disabled({"DIP_AGENTS_WRITE_DISABLED": value})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_the_switch_is_off_otherwise(value):
    assert not writes_disabled({"DIP_AGENTS_WRITE_DISABLED": value})
    assert not writes_disabled({})


def test_on_it_refuses_an_agents_propose_and_act_calls(monkeypatch):
    monkeypatch.setenv("DIP_AGENTS_WRITE_DISABLED", "1")
    r = registry()
    assert r.check(AGENT, "run", "read_x", {}) is None
    for name in ("propose_x", "act_x"):
        assert "switched off" in r.check(AGENT, "run", name, {})


def test_on_it_leaves_people_alone(monkeypatch):
    monkeypatch.setenv("DIP_AGENTS_WRITE_DISABLED", "1")
    assert registry().check(human_policy("alice"), None, "act_x", {}) is None


def test_off_the_agent_may_write(monkeypatch):
    monkeypatch.delenv("DIP_AGENTS_WRITE_DISABLED", raising=False)
    assert registry().check(AGENT, "run", "propose_x", {}) is None


def test_on_the_runners_claim_nothing():
    mcp = {"DIP_MCP_URL": "u", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}
    p = chain_job.plan({**mcp, "GEMINI_API_KEY": "g", "DIP_AGENTS_WRITE_DISABLED": "1"})
    assert not p.runnable and "switched off" in p.reason
    keys = {v: "k" for v in ("DIP_MCP_URL", *intake_runner.KEYS.values())}
    providers, why = intake_runner.plan(
        {**keys, "GEMINI_API_KEY": "g", "DIP_AGENTS_WRITE_DISABLED": "true"}
    )
    assert providers == () and "switched off" in why
