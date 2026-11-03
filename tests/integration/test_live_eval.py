"""Stage 11 (S11.11): the live eval's comparison. The baseline is the recordings replayed
through each agent's own harness; a seeded drift (a test copy of a case with a changed
expected answer) fails against it, as a model that changed its answer would; and an agent
costing more than twice its baseline is drift too. Replays only: no key, no cost."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("live_eval", ROOT / "scripts" / "live_eval.py")
live_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_eval)


def test_the_committed_baseline_covers_the_sample_and_every_case_passed():
    baseline = json.loads(live_eval.BASELINE.read_text("utf-8"))
    assert baseline["sample"] == live_eval.SAMPLE
    assert {(c["agent"], c["case"]) for c in baseline["cases"]} == {
        (a, n) for a, names in live_eval.SAMPLE.items() for n in names}  # fmt: skip
    assert all(c["passed"] and c["calls"] > 0 and c["cost_usd"] > 0 for c in baseline["cases"])


def test_a_seeded_drift_fails_against_the_baseline(warehouse):
    """The incident agent's case, replayed, but its expected root cause changed: it passed
    when recorded and fails now, so the comparison reports drift."""
    from tests.integration.conftest import DSN

    case = copy.deepcopy(live_eval._cases("incident")["platform_oomkilled"])
    case["gold"]["root_cause"] = ["platform_scheduler"]  # what a drifted answer would miss
    r = live_eval.measure("incident", case, DSN, live=False)
    assert r["passed"] is False and r["calls"] > 0
    baseline = json.loads(live_eval.BASELINE.read_text("utf-8"))
    findings, md = live_eval.compare(baseline, [r])
    assert findings == ["incident/platform_oomkilled: passed when recorded, fails live"]
    assert "**drift**" in md


def test_an_agent_over_twice_its_baseline_cost_is_drift():
    baseline = {"cases": [{"agent": "reviewer", "case": "x", "passed": True, "turns": 3,
                           "calls": 3, "cost_usd": 0.002}]}  # fmt: skip
    live = [{"agent": "reviewer", "case": "x", "passed": True, "turns": 3, "calls": 9,
             "cost_usd": 0.0041}]  # fmt: skip
    findings, _ = live_eval.compare(baseline, live)
    assert findings == ["reviewer: live cost $0.0041 is over 2x the baseline's $0.0020"]
    live[0]["cost_usd"] = 0.0039
    assert live_eval.compare(baseline, live)[0] == []


def test_a_case_that_cannot_run_is_a_failure_on_its_row_not_a_crash():
    r = live_eval.measure("reviewer", "no_such_case", "postgresql://unused", live=False)
    assert r["passed"] is False and r["error"].startswith("KeyError")
