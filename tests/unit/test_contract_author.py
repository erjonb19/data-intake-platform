"""Stage 5 (S5.1): the contract author's plain-code parts. A file observed against a
contract (with no PHI values out), a candidate's dry run, what a candidate loosens, and the
human-approval step that writes an approved contract to a local branch."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from dip.contract_author import dry_run, loosening, observe, shape
from dip.contracts import ContractError, load_contracts, parse_contract

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"


@pytest.fixture(scope="module")
def elig():
    return load_contracts()["healthcare.eligibility"]


def test_observe_shows_no_values_of_phi_or_undeclared_columns(elig):
    lines = (FIX / "good/elig_good.csv").read_text("utf-8").splitlines()
    lines[0] = lines[0].replace("dob", "DOB")  # renamed: undeclared now
    seen = observe(elig, ("\n".join(lines) + "\n").encode())
    cols = {c["column"]: c for c in seen["columns"]}
    assert seen["missing_declared"] == ["dob"] and seen["unexpected"] == ["DOB"]
    assert "top_values" not in cols["first_name"] and "top_values" not in cols["DOB"]
    assert cols["DOB"]["inferred_type"] == "date" and cols["DOB"]["shapes"][0][0] == "9999-99-99"
    assert "codes" not in cols["DOB"]  # dates are not code-like
    assert dict(cols["plan_code"]["top_values"])  # declared, not PHI: values shown


def test_observe_of_a_new_vendor_gives_codes_only_for_code_like_columns():
    seen = observe(None, (FIX / "bad/elig_bad_rows.csv").read_bytes())
    cols = {c["column"]: c for c in seen["columns"]}
    assert cols["sex"]["codes"] == ["F", "M"]
    assert "GOLD" in cols["plan_code"]["codes"]
    assert "codes" not in cols["first_name"] and "codes" not in cols["member_id"]
    assert shape("M000000001") == "A999999999" and shape("Alex") == "Aaaa"


def test_rule_failures_carry_examples_only_when_not_phi(elig):
    seen = observe(elig, (FIX / "bad/elig_bad_rows.csv").read_bytes())
    plan = next(c for c in seen["columns"] if c["column"] == "plan_code")
    assert plan["rule_failures"]["allowed"] == {"count": 1, "examples": ["GOLD"]}


def test_dry_run_counts_like_the_intake(elig):
    assert dry_run(elig, (FIX / "good/elig_good.csv").read_bytes()) == {
        "file_rejected": False, "accepted": 6, "rejected": 0, "reason_codes": {}}  # fmt: skip
    got = dry_run(elig, (FIX / "bad/elig_bad_rows.csv").read_bytes())
    assert got["rejected"] == 1 and got["reason_codes"] == {"VALUE.NOT_ALLOWED:plan_code": 1}


def test_loosening_names_every_rule_a_candidate_drops_or_widens(elig):
    old = elig.spec
    new = copy.deepcopy(old)
    cols = {c["name"]: c for c in new["columns"]}
    cols["plan_code"]["allowed"].append("GOLD")  # the vendor's legitimate change
    assert [(x["where"], x["rule"]) for x in loosening(old, new)] == [("plan_code", "allowed")]
    cols["member_id"].pop("pattern")
    cols["first_name"]["required"] = False
    cols["dob"]["phi"] = False
    cols["dob"]["min"] = "1800-01-01"
    new["max_bad_row_ratio"] = 1.0
    rules = {(x["where"], x["rule"]) for x in loosening(old, new)}
    assert rules >= {("member_id", "pattern"), ("first_name", "required"), ("dob", "phi"),
                     ("dob", "min"), ("contract", "max_bad_row_ratio")}  # fmt: skip
    assert loosening(old, copy.deepcopy(old)) == []  # unchanged: nothing loosened


def test_a_candidate_is_checked_by_every_rule_and_aliases_are_refused(elig, tmp_path):
    text = elig.path.read_text("utf-8")
    assert parse_contract(text, tmp_path / "x.yaml").feed == "healthcare.eligibility"
    with pytest.raises(ContractError, match="aliases"):
        parse_contract("a: &x [1, 2]\nb: *x\n", tmp_path / "x.yaml")
    with pytest.raises(ContractError):
        parse_contract(text.replace("type: csv", "type: parquet"), tmp_path / "x.yaml")
    with pytest.raises(ContractError, match="characters"):
        parse_contract("#" * 70000, tmp_path / "x.yaml")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def test_an_approved_contract_becomes_a_local_branch_and_nothing_else(elig, tmp_path, monkeypatch):
    from dip.agents.contract_pr import text_sha256, write_branch

    repo = tmp_path / "repo"
    (repo / "packs/healthcare/contracts").mkdir(parents=True)
    original = elig.path.read_text("utf-8")
    (repo / "packs/healthcare/contracts/eligibility.yaml").write_text(original, "utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
    candidate = original.replace("[HMO, PPO, EPO, HDHP]", "[HMO, PPO, EPO, HDHP, GOLD]")
    assert candidate != original
    payload = {"feed": "healthcare.eligibility", "mode": "drift",
               "path": "packs/healthcare/contracts/eligibility.yaml",
               "base_sha256": text_sha256(original), "contract_yaml": candidate,
               "rationale": "The vendor added plan GOLD.",
               "validation": {}, "loosening": []}  # fmt: skip
    _git(repo, "checkout", "-q", "-b", "my-feature")
    (repo / "notes.txt").write_text("unrelated work\n", "utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "wip")
    main = _git(repo, "rev-parse", "main")
    head = _git(repo, "rev-parse", "HEAD")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    got = write_branch(payload, 7, "human:tester", root=repo)
    assert got["branch"] == "contract/healthcare.eligibility-approval-7"
    assert "GOLD" in _git(repo, "show", f"{got['branch']}:{payload['path']}")
    # Cut from main, not from the person's feature branch; committed with LF only.
    assert _git(repo, "rev-parse", f"{got['branch']}^") == main
    assert "notes.txt" not in _git(repo, "ls-tree", "--name-only", "-r", got["branch"])
    blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob",
                           f"{got['branch']}:{payload['path']}"], capture_output=True,
                          check=True).stdout  # fmt: skip
    assert b"\r\n" not in blob
    # The person's checkout is untouched: same branch, same HEAD, clean tree.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "my-feature"
    assert _git(repo, "rev-parse", "HEAD") == head and _git(repo, "status", "--porcelain") == ""
    assert got["next"][0].startswith("git push -u origin contract/")
    assert got["next"][1].startswith("gh pr create --base main --head contract/")
    # Proposed against an older file: refused.
    with pytest.raises(RuntimeError, match="changed since"):
        write_branch({**payload, "base_sha256": "0" * 64}, 8, "human:tester", root=repo)
    # A candidate that is not a contract, or not under packs/: refused.
    with pytest.raises(ContractError):
        write_branch({**payload, "contract_yaml": "feed: x\n"}, 9, "human:tester", root=repo)
    with pytest.raises(ContractError, match="under packs"):
        write_branch({**payload, "path": "../evil.yaml"}, 10, "human:tester", root=repo)


def test_loosening_catches_what_the_review_found(elig):
    old = elig.spec
    new = copy.deepcopy(old)
    new["allow_extra_columns"] = True
    new["min_rows"] = 0
    new["classification"] = "internal"
    new["source"]["channels"].append("sftp") if "sftp" not in new["source"]["channels"] else None
    new["source"]["filename_pattern"] = "*.csv"
    new["columns"] = [c for c in new["columns"] if c["name"] != "coverage_start"]
    cols = {c["name"]: c for c in new["columns"]}
    cols["dob"]["max"] = "2099-01-01"  # was today: later, so wider
    rules = {(x["where"], x["rule"]) for x in loosening(old, new)}
    assert rules >= {("contract", "allow_extra_columns"), ("contract", "min_rows"),
                     ("contract", "classification"), ("source", "filename_pattern"),
                     ("coverage_start", "dropped"), ("dob", "max")}  # fmt: skip


def test_a_wider_type_is_a_loosening():
    old = {"columns": [{"name": "n", "type": "integer"}, {"name": "d", "type": "date"}]}
    new = {"columns": [{"name": "n", "type": "decimal"}, {"name": "d", "type": "string"}]}
    assert {(x["where"], x["rule"]) for x in loosening(old, new)} == {("n", "type"), ("d", "type")}
    assert loosening(new, old) == []  # narrowing is not


def test_digit_codes_are_short_and_dates_or_zips_never_codes():
    from dip.contract_author import CODE

    assert CODE.match("GOLD") and CODE.match("11") and CODE.match("90") and CODE.match("USD")
    assert not CODE.match("19800101") and not CODE.match("10001") and not CODE.match("1234")
    lines = ["id,birth_dt,zip", *[f"{i},1980010{i % 3 + 1},1000{i % 2 + 1}" for i in range(24)]]
    seen = observe(None, ("\n".join(lines) + "\n").encode())
    cols = {c["column"]: c for c in seen["columns"]}
    assert "codes" not in cols["birth_dt"] and "codes" not in cols["zip"]


def test_a_bom_is_read_as_the_intake_reads_it(elig):
    data = "﻿".encode() + (FIX / "good/elig_good.csv").read_bytes()
    seen = observe(elig, data)
    assert seen["unexpected"] == [] and seen["missing_declared"] == []


def test_the_dry_run_checks_the_file_name_like_the_intake(elig):
    from datetime import UTC, datetime

    data = (FIX / "good/elig_good.csv").read_bytes()
    at = datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert not dry_run(elig, data, at, "elig_20261021.csv")["file_rejected"]
    got = dry_run(elig, data, at, "members_20261021.csv")
    assert got["file_rejected"] and "FILE.NAME_PATTERN" in got["reason_codes"]
