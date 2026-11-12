"""The agent_chain DAG's decision to run, skip, or refuse (dip/agents/chain_job.py), with
no database: the environment alone decides it."""

from __future__ import annotations

from dip.agents import chain_job

MCP = {"DIP_MCP_URL": "http://mcp:8765/mcp", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}
CAPS = {"DIP_SPEND_CAP_USD": "0.25", "DIP_MONTHLY_CAP_USD": "5"}


def test_no_model_key_means_skip_not_crash():
    p = chain_job.plan(MCP)
    assert not p.runnable and "no model key" in p.reason


def test_missing_mcp_configuration_names_what_is_unset():
    p = chain_job.plan({"DIP_MCP_URL": "http://mcp:8765/mcp", "ANTHROPIC_API_KEY": "x"})
    assert not p.runnable
    assert p.reason == "no MCP configuration: DIP_INCIDENT_KEY, DIP_TRIAGE_KEY unset"


def test_anthropic_is_tried_first_and_gemini_after_it():
    both = {**MCP, "ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", **CAPS}
    assert chain_job.plan(both).providers == ("anthropic", "gemini")
    assert chain_job.plan({**MCP, "GEMINI_API_KEY": "g"}).providers == ("gemini",)


def test_an_empty_key_counts_as_unset():
    assert not chain_job.plan({**MCP, "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}).runnable


def test_a_paid_key_without_both_spend_caps_claims_nothing():
    # 24.3: fails closed; the incidents wait for a configuration with caps.
    for env in ({}, {"DIP_SPEND_CAP_USD": "0.25"}, {"DIP_MONTHLY_CAP_USD": "5"}):
        p = chain_job.plan({**MCP, "ANTHROPIC_API_KEY": "a", **env})
        assert not p.runnable
        assert p.reason and p.reason.startswith("a paid model key with no spend cap")
    assert chain_job.plan({**MCP, "ANTHROPIC_API_KEY": "a", **CAPS}).runnable


def test_a_free_provider_alone_needs_no_cap():
    assert chain_job.plan({**MCP, "GEMINI_API_KEY": "g"}).runnable
