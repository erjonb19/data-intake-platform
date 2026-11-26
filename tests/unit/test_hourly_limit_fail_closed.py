"""24.31: when the last hour's calls cannot be read from the audit log, the call is refused
and audited as denied, and the connection is left usable."""

from __future__ import annotations

import pytest

from dip.agents.registry import Denied, Policy, Registry, Tool


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "from ops.audit_log" in sql and sql.lstrip().startswith("select"):
            self.conn.aborted = True
            raise RuntimeError("permission denied for table audit_log")
        assert not self.conn.aborted, "a statement on an aborted transaction"
        self.conn.inserts.append(params)


class Conn:
    def __init__(self):
        self.inserts, self.aborted, self.rollbacks = [], False, 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        assert not self.aborted

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1


def test_an_unreadable_count_refuses_the_call_and_audits_it():
    conn = Conn()
    reg = Registry(conn)
    reg.register(Tool("read_x", "read", "", {"type": "object", "properties": {}}, lambda **_: "ok"))
    limited = Policy("triage", "read", frozenset({"*"}), limits={"max_calls_per_hour": 5})
    with pytest.raises(Denied, match="could not be counted"):
        reg.call(limited, "r1", "read_x", {})
    assert conn.rollbacks == 1
    assert [p[5] for p in conn.inserts] == ["denied"]  # the audit row's decision


def test_a_policy_without_hourly_limits_never_reads_the_log():
    conn = Conn()
    reg = Registry(conn)
    reg.register(Tool("read_x", "read", "", {"type": "object", "properties": {}}, lambda **_: "ok"))
    unlimited = Policy("triage", "read", frozenset({"*"}))
    assert reg.call(unlimited, "r1", "read_x", {}) == "ok"
    assert conn.rollbacks == 0
