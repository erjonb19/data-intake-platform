"""Stage 5: the contract author end to end, without a model. A scripted stand-in plays the
agent over the real MCP server, policy and tools: it reads the drift, validates a candidate,
and proposes it. Then a person approves and executes it, which writes a local branch. The
scorer is checked on a good and a loosening candidate, and the policy on a hijacked one.
The recorded live runs replay in test_contract_author_evals below."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dip.agents import author_evals
from dip.agents.llm import Message, Recording
from tests.integration.conftest import DSN

CASES = {c["name"]: c for c in author_evals.load_cases()}


class Scripted:
    """Turns of a pretend model: each a list of (tool, args) to call, then a final text.
    `edit` turns the current contract text into the candidate."""

    def __init__(self, edit, extra_calls=()):
        self.edit, self.extra, self.turn = edit, list(extra_calls), 0

    def create(self, *, system, tools, messages):
        self.turn += 1
        last = messages[-1]["content"]
        results = [json.loads(b["content"]) if b["content"][:1] == "{" else b["content"]
                   for b in last if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(last, list) else []  # fmt: skip
        feed = "healthcare.eligibility"
        sha = self.sha = getattr(self, "sha", None) or _sha_from(messages[0]["content"])
        if self.turn == 1:
            calls = [("diff_schema", {"feed": feed, "file_sha256": sha}),
                     ("get_contract", {"feed": feed}), *self.extra]  # fmt: skip
        elif self.turn == 2:
            self.yaml = self.edit()
            calls = [("validate_contract", {"feed": feed, "contract_yaml": self.yaml,
                                            "file_sha256": sha})]  # fmt: skip
        elif self.turn == 3:
            assert results and results[0]["valid"], results
            cid = results[0]["candidate_id"]
            calls = [("propose_contract_pr",
                      {"candidate_id": cid, "mode": "drift", "rationale": "GOLD."})]  # fmt: skip
        else:
            return Message([{"type": "text", "text": "Proposed."}], "end_turn")
        return Message([{"type": "tool_use", "id": f"t{self.turn}-{i}", "name": n, "input": a}
                        for i, (n, a) in enumerate(calls)], "tool_use")  # fmt: skip


def _sha_from(task: str) -> str:
    import re

    return re.search(r"[0-9a-f]{64}", task)[0]


def _current() -> str:
    return (author_evals.EVALS / "contracts" / "eligibility.yaml").read_text("utf-8")


def _gold_added():
    return _current().replace("[HMO, PPO, EPO, HDHP]", "[HMO, PPO, EPO, HDHP, GOLD]").replace(
        "version: 1", "version: 2")  # fmt: skip


@pytest.fixture
def work(tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    return tmp_path


def test_a_scripted_agent_proposes_and_the_case_passes(work):
    r = author_evals.run_case(CASES["drift_new_plan_code"], DSN, Scripted(_gold_added), work)
    assert r.proposed and r.decision_ok and r.passed, r
    assert [c["ok"] for c in r.checks] == [True] and r.loosening_errors == []


def test_a_loosening_candidate_fails_the_case(work):
    def loose():
        return _gold_added().replace("max_bad_row_ratio: 0.05", "max_bad_row_ratio: 0.5")

    r = author_evals.run_case(CASES["drift_new_plan_code"], DSN, Scripted(loose), work)
    assert r.proposed and not r.passed
    assert [(x["where"], x["rule"]) for x in r.loosening_errors] == [
        ("contract", "max_bad_row_ratio")
    ]


def test_a_hijacked_agent_cannot_act_or_use_other_tools(work):
    extra = [("execute_approved_operation", {"approval_id": 1}),
             ("propose_operation", {"action": "discard", "group_id": "x|y",
                                    "quarantine_ids": [1], "root_cause": "unknown",
                                    "rationale": "x", "confidence": 1})]  # fmt: skip
    r = author_evals.run_case(CASES["drift_new_plan_code"], DSN, Scripted(_gold_added, extra),
                              work)  # fmt: skip
    assert len(r.violations) == 2 and not r.passed


def test_an_approved_proposal_becomes_a_local_branch(work, tmp_path, monkeypatch):
    """Propose (scripted), approve as a person, execute: a branch in a throwaway clone."""
    import psycopg

    from dip.agents import approvals
    from dip.contracts import REPO_ROOT

    r = author_evals.run_case(CASES["drift_new_plan_code"], DSN, Scripted(_gold_added), work)
    assert r.proposed
    with psycopg.connect(DSN) as conn:
        [a] = [x for x in approvals.pending(conn) if x["action"] == "contract_pr"]
        payload = dict(a["payload"])
        # The eval world's contract lives in the evals folder; for the branch step, point
        # the proposal at a throwaway repo whose packs/ holds that same file.
        repo = tmp_path / "clone"
        rel = "packs/healthcare/contracts/eligibility.yaml"
        (repo / rel).parent.mkdir(parents=True)
        (repo / rel).write_text(_current(), "utf-8")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@example.com", *args], check=True)  # fmt: skip
        for who in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{who}_NAME", "t")
            monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
        from dip.agents.contract_pr import write_branch

        assert payload["loosening"] == [{"where": "plan_code", "rule": "allowed",
                                         "from": ["HMO", "PPO", "EPO", "HDHP"],
                                         "to": ["HMO", "PPO", "EPO", "HDHP", "GOLD"]}]  # fmt: skip
        got = write_branch({**payload, "path": rel}, a["approval_id"], "human:tester", repo)
        show = subprocess.run(["git", "-C", str(repo), "show", f"{got['branch']}:{rel}"],
                              capture_output=True, text=True, check=True).stdout  # fmt: skip
        assert "GOLD" in show and "version: 2" in show
        assert REPO_ROOT.exists()  # the real repo was not touched (write_branch got `repo`)


RECORDED = sorted(p.stem for p in author_evals.RECORDINGS.glob("*.json"))


@pytest.mark.parametrize("name", RECORDED)
def test_contract_author_evals(work, name):
    """The recorded live runs, replayed: no model calls."""
    r = author_evals.run_case(CASES[name], DSN, Recording(author_evals.RECORDINGS /
                                                          f"{name}.json"), work)  # fmt: skip
    assert r.passed, author_evals.report([r])["cases"][0]


def test_every_eval_file_is_generated_from_tracked_fixtures():
    for case in CASES.values():
        name, data = author_evals.case_file(case)
        assert data and name
    assert {c["mode"] for c in CASES.values()} == {"drift", "onboard"}
    assert Path(author_evals.EVALS / "reference").is_dir()
