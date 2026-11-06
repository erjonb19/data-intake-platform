"""Stage 12 (12.1, #83): the nightly evals' check (scripts/check_eval_reports.py) compares
the evals the replay step started with the reports they wrote, instead of a fixed count."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_eval_reports.py"
spec = importlib.util.spec_from_file_location("check_eval_reports", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def write(reports: Path, name: str, passed: int, total: int, keys=("cases_passed", "cases")):
    (reports / name).mkdir(parents=True)
    body = {"summary": {keys[0]: passed, keys[1]: total}}
    (reports / name / "report.json").write_text(json.dumps(body), "utf-8")


def expect(reports: Path, *names: str):
    reports.mkdir(exist_ok=True)
    (reports / "expected.txt").write_text("".join(f"{n}\n" for n in names), "utf-8")


def test_seven_evals_all_passing_pass(tmp_path):
    """The case that failed nightly from 2026-11-03: seven reports, every case passing."""
    names = [f"e{i}" for i in range(7)]
    expect(tmp_path, *names)
    for n in names:
        write(tmp_path, n, 3, 3)
    assert check.main([str(tmp_path)]) == 0


def test_the_loop_report_shape_counts(tmp_path):
    expect(tmp_path, "break-loop")
    write(tmp_path, "break-loop", 8, 8, keys=("passed", "scenarios"))
    assert check.main([str(tmp_path)]) == 0


def test_a_failed_case_fails(tmp_path):
    expect(tmp_path, "triage", "incident")
    write(tmp_path, "triage", 5, 5)
    write(tmp_path, "incident", 9, 10)
    assert check.problems(tmp_path) == ["incident: 9/10 cases passed"]


def test_an_eval_that_wrote_no_report_fails(tmp_path):
    expect(tmp_path, "triage", "incident")
    write(tmp_path, "triage", 5, 5)
    assert check.problems(tmp_path) == ["incident: started, but wrote no report"]


def test_a_report_nobody_started_fails(tmp_path):
    expect(tmp_path, "triage")
    write(tmp_path, "triage", 5, 5)
    write(tmp_path, "stale", 1, 1)
    assert check.main([str(tmp_path)]) == 1


def test_zero_cases_and_unreadable_reports_fail(tmp_path):
    expect(tmp_path, "empty", "broken")
    write(tmp_path, "empty", 0, 0)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "report.json").write_text("{}", "utf-8")
    bad = check.problems(tmp_path)
    assert bad[0] == "empty: 0/0 cases passed"
    assert bad[1].startswith("broken: unreadable report")


def test_a_null_or_list_summary_is_reported_not_raised(tmp_path):
    expect(tmp_path, "null", "list")
    for name, body in (("null", '{"summary": null}'), ("list", '{"summary": []}')):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(body, "utf-8")
    assert [b.split(":")[0] for b in check.problems(tmp_path)] == ["null", "list"]


def test_no_listing_fails(tmp_path):
    write(tmp_path, "triage", 5, 5)
    assert check.main([str(tmp_path)]) == 1


def test_the_workflow_lists_every_eval_it_runs_and_uses_this_check():
    """Every `run <name> ...` line in evals.yml goes through run(), which appends the name
    to expected.txt, and the last check is this script, not a count."""
    wf = (ROOT / ".github" / "workflows" / "evals.yml").read_text("utf-8")
    assert re.search(r'echo "\$name" >> reports/expected\.txt', wf)
    assert "python scripts/check_eval_reports.py reports" in wf
    assert "expected 6" not in wf
    runs = re.findall(r"^\s+run ([a-z-]+) ", wf, flags=re.M)
    assert len(runs) == len(set(runs)) >= 7
