"""Stage 8 (S8.2): the recorded demo replays end to end with no model call: the breakage,
the incident, the LangGraph handoff, triage, a refused and then an approved Act call, the
resend, and the audit trail."""

from __future__ import annotations

import pytest

from dip import demo
from tests.integration.conftest import DSN


def test_the_recorded_demo_runs_end_to_end(tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    lines: list[str] = []
    assert demo.run(DSN, tmp_path / "store", say=lines.append) == 0
    text = "\n".join(lines)
    assert "intake: quarantined ['SCHEMA.MISSING_COLUMN:member_id']" in text
    assert (
        "handoff (decided in code): ['healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id']"
        in text
    )
    assert "execute_approved_operation  denied" in text
    assert "intake: loaded, 1 quarantine entries superseded" in text
    assert "incident 1: closed" in text and lines[-1].endswith("demo complete")
