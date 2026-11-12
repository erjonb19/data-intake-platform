"""24.8: a write call refused by the kill switch is audited as denied, and nothing runs."""

from __future__ import annotations

import pytest

from dip.agents.registry import Denied, Policy, Registry, Tool
from tests.integration.conftest import rows


def test_a_switched_off_write_is_denied_audited_and_not_run(warehouse, monkeypatch):
    monkeypatch.setenv("DIP_AGENTS_WRITE_DISABLED", "1")
    ran = []
    r = Registry(warehouse.conn)
    schema = {"type": "object", "properties": {}}
    r.register(Tool("propose_x", "propose", "", schema, lambda: ran.append(1)))
    with pytest.raises(Denied, match="switched off"):
        r.call(Policy("triage", "act", frozenset({"*"})), "chain-k/triage", "propose_x", {})
    assert ran == []
    got = rows(warehouse, "select principal, tool, tier, decision, reason from ops.audit_log")
    assert got == [
        (
            "triage",
            "propose_x",
            "propose",
            "denied",
            "agent write tools are switched off (DIP_AGENTS_WRITE_DISABLED)",
        )
    ]
