"""24.10: signed chain tokens (dip/agents/run_token.py), with no server."""

from __future__ import annotations

from dip.agents import run_token

ENV = {run_token.KEY_VAR: "k" * 40}


def test_without_a_key_nothing_is_signed_or_checked():
    assert run_token.sign("chain-a", env={}) is None
    assert run_token.verify("chain-a", None, env={}) is None


def test_a_run_id_with_no_chain_needs_no_token():
    assert run_token.verify(None, None, env=ENV) is None


def test_a_token_holds_only_for_its_chain_and_until_it_expires():
    t = run_token.sign("chain-a", now=1000, env=ENV)
    assert run_token.verify("chain-a", t, now=1000 + 60, env=ENV) is None
    assert "not for this chain" in run_token.verify("chain-b", t, now=1060, env=ENV)
    assert "expired" in run_token.verify("chain-a", t, now=1000 + run_token.TTL + 1, env=ENV)


def test_a_missing_malformed_or_edited_token_is_refused():
    t = run_token.sign("chain-a", env=ENV)
    expires, _, mac = t.partition(".")
    assert "no run token" in run_token.verify("chain-a", None, env=ENV)
    assert "malformed" in run_token.verify("chain-a", "abc", env=ENV)
    later = f"{int(expires) + 3600}.{mac}"  # a longer life, same signature
    assert "not for this chain" in run_token.verify("chain-a", later, env=ENV)


def test_another_key_signs_nothing_this_one_accepts():
    t = run_token.sign("chain-a", env={run_token.KEY_VAR: "x" * 40})
    assert run_token.verify("chain-a", t, env=ENV) is not None


def test_a_bare_run_id_is_its_own_chain():
    """Tools fall back to the raw run id as the chain, so `intake-y` needs a token too."""
    assert run_token.chain_for("intake-y") == "intake-y"
    assert run_token.chain_for("intake-y/map#2") == "intake-y"
    assert run_token.chain_for(None) is None
    assert "no run token" in run_token.verify(run_token.chain_for("intake-y"), None, env=ENV)


def test_odd_characters_fail_closed_without_an_exception():
    for token in ("²³.abc", "123.é", ".", "abc.def"):
        assert run_token.verify("chain-a", token, env=ENV) is not None
