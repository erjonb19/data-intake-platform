"""13.3's prerequisite: the proof workflow's commit-results job commits only to a stage
branch, s11-* to s29-* (it was s11-* only). It has contents: write, so `main`, tags and any
other branch must stay refused."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROOF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "proof.yml"


def job() -> dict:
    return yaml.safe_load(PROOF.read_text(encoding="utf-8"))["jobs"]["commit-results"]


def branch_regex() -> re.Pattern[str]:
    script = job()["steps"][0]["run"]
    m = re.search(r'\[\[ "\$BRANCH" =~ (\S+) \]\]', script)
    assert m, "commit-results no longer checks the branch with a regex"
    return re.compile(m.group(1))  # bash ERE; this one is also a valid Python pattern


def test_stage_branches_are_accepted():
    rx = branch_regex()
    for b in ("s11-10-drills", "s13-3-bad-release", "s19-1-scale", "s29-x"):
        assert rx.fullmatch(b), b


def test_main_and_other_names_are_refused():
    rx = branch_regex()
    for b in ("main", "s1-x", "s10-x", "s30-x", "s13", "s13-", "owner-steps",
              "s13-x;rm", "s13-x y", "xs13-x"):  # fmt: skip
        assert not rx.fullmatch(b), b


def test_the_job_condition_admits_stage_branches_and_needs_success():
    cond = job()["if"]
    assert "github.ref_type == 'branch'" in cond
    assert "startsWith(github.ref_name, 's1')" in cond
    assert "startsWith(github.ref_name, 's2')" in cond
    assert "!contains(needs.*.result, 'failure')" in cond
    assert job()["permissions"] == {"contents": "write"}
