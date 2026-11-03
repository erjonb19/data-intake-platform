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


def test_the_baseline_is_the_recordings_replayed_now(warehouse):
    """Re-recording a sampled case means writing the baseline again
    (`python scripts/live_eval.py baseline`): this fails until it is."""
    from tests.integration.conftest import DSN

    baseline = json.loads(live_eval.BASELINE.read_text("utf-8"))
    assert live_eval.sample(DSN, live=False) == baseline["cases"]


def test_one_case_over_twice_its_baseline_cost_is_drift_though_its_agent_is_not():
    row = {"agent": "triage", "passed": True, "turns": 4, "calls": 4, "cost_usd": 0.0023}
    baseline = {"cases": [dict(row, case="a"), dict(row, case="b")]}
    live = [
        dict(baseline["cases"][0], cost_usd=0.0069),
        dict(baseline["cases"][1], cost_usd=0.0001),
    ]
    findings, md = live_eval.compare(baseline, live)
    assert findings == ["triage/a: live cost $0.0069 is over 2x the baseline's $0.0023"]
    assert "**drift**" in md


def _seeded_drift() -> dict:
    """The incident agent's case, replayed, but its expected root cause changed."""
    from tests.integration.conftest import DSN

    case = copy.deepcopy(live_eval._cases("incident")["platform_oomkilled"])
    case["gold"]["root_cause"] = ["platform_scheduler"]  # what a drifted answer would miss
    return live_eval.measure("incident", case, DSN, live=False)


def test_a_seeded_drift_fails_against_the_baseline(warehouse):
    """It passed when recorded and fails now, so the comparison reports drift."""
    r = _seeded_drift()
    assert r["passed"] is False and r["calls"] > 0
    baseline = json.loads(live_eval.BASELINE.read_text("utf-8"))
    findings, md = live_eval.compare(baseline, [r])
    assert findings == ["incident/platform_oomkilled: passed when recorded, fails live"]
    assert "**drift**" in md


def test_a_seeded_drift_fails_the_job(warehouse, monkeypatch, tmp_path):
    """The job's own command (`live OUT_DIR`) on the seeded drift: exit 1, drift.md written."""
    r = _seeded_drift()
    monkeypatch.setenv("DIP_EVAL_DSN", "postgresql://unused")
    monkeypatch.setattr(live_eval, "sample", lambda dsn, live: [r])
    assert live_eval.main(["live", str(tmp_path)]) == 1
    assert "passed when recorded, fails live" in (tmp_path / "drift.md").read_text("utf-8")


def test_an_agent_over_twice_its_baseline_cost_is_drift():
    baseline = {"cases": [{"agent": "reviewer", "case": "x", "passed": True, "turns": 3,
                           "calls": 3, "cost_usd": 0.002}]}  # fmt: skip
    live = [{"agent": "reviewer", "case": "x", "passed": True, "turns": 3, "calls": 9,
             "cost_usd": 0.0041}]  # fmt: skip
    findings, _ = live_eval.compare(baseline, live)
    over = "live cost $0.0041 is over 2x the baseline's $0.0020"
    assert findings == [f"reviewer/x: {over}", f"reviewer: {over}"]
    live[0]["cost_usd"] = 0.0039
    assert live_eval.compare(baseline, live)[0] == []


def test_a_case_that_cannot_run_is_a_failure_on_its_row_not_a_crash():
    r = live_eval.measure("reviewer", "no_such_case", "postgresql://unused", live=False)
    assert r["passed"] is False and r["error"].startswith("KeyError")
