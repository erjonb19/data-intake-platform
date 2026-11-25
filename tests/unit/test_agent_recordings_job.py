"""The proof workflow's claude-agent-recordings job (24.17): the triage agent's evals and new
reviewer cases recorded on Claude under one cap, scrubbed, checked, replayed; and
commit-results takes only recordings from it."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROOF = ROOT / ".github" / "workflows" / "proof.yml"


def wf() -> dict:
    return yaml.safe_load(PROOF.read_text(encoding="utf-8"))


def test_it_is_a_manual_choice_with_a_cap_of_60_cents():
    w = wf()
    assert "claude-agent-recordings" in w[True]["workflow_dispatch"]["inputs"]["job"]["options"]
    job = w["jobs"]["claude-agent-recordings"]
    assert job["if"] == "inputs.job == 'claude-agent-recordings'"
    record = next(s for s in job["steps"] if "record_claude_agents.sh" in s.get("run", ""))
    assert record["env"]["DIP_SPEND_CAP_USD"] == "${{ inputs.spend_cap_usd }}"
    assert "> 0.60)" in record["run"]
    assert 'grep -x "scrub: ok"' in record["run"]
    assert "check_recordings.py --key-env ANTHROPIC_API_KEY" in record["run"]
    replay = next(s for s in job["steps"] if "replay check: ok" in s.get("run", ""))
    assert "secrets." not in yaml.safe_dump(replay)
    assert "check_eval_reports.py reports" in replay["run"]


def test_commit_results_takes_only_recordings_from_it():
    job = wf()["jobs"]["commit-results"]
    assert "claude-agent-recordings" in job["needs"]
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'elif [ "$AGENTS" = success ]; then name=claude-agent-recordings' in script
    allowed = re.compile(re.search(r"allowed='([^']+)'", script).group(1))
    for ok in ("agents/evals/triage/recordings/missing_column.json",
               "agents/evals/triage/recordings/gemini/missing_column.json",
               "agents/evals/triage_act/recordings/x.json",
               "agents/evals/break_loop/recordings/x.json",
               "agents/evals/reviewer/recordings/x.json"):  # fmt: skip
        assert allowed.fullmatch(ok), ok
    for bad in ("agents/evals/triage/cases.yaml", "agents/prompts/triage.md",
                "agents/policies/triage.yaml", "agents/evals/triage/recordings/../x.json",
                "agents/evals/contract_author/recordings/x.json"):  # fmt: skip
        assert not allowed.fullmatch(bad), bad


def test_the_script_records_every_triage_set_in_one_process():
    text = (ROOT / "scripts" / "record_claude_agents.sh").read_text(encoding="utf-8")
    wanted = [
        '"triage", "eval", "--record"',
        "holdout.yaml",
        "from_rejections.yaml",
        '"act-eval", "--record"',
        '"loop", "--record"',
        '"review-eval", "--record"',
    ]
    for argv in wanted:
        assert argv in text, argv
    assert text.count("<<'EOF'") == 2  # the recording (one process) and the scrub
    assert "ANTHROPIC_API_KEY" in text and "scrub: ok" in text
