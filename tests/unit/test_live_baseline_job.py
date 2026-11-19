"""The proof workflow's live-baseline job (2026-11-18): rewrites agents/evals/baseline.json
from the committed recordings, replayed, on a runner, with no model key and no spend; and
commit-results takes that one file from it and nothing else."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROOF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "proof.yml"


def wf() -> dict:
    return yaml.safe_load(PROOF.read_text(encoding="utf-8"))


def test_it_is_a_manual_choice_and_holds_no_secret():
    w = wf()
    assert "live-baseline" in w[True]["workflow_dispatch"]["inputs"]["job"]["options"]
    job = w["jobs"]["live-baseline"]
    assert job["if"] == "inputs.job == 'live-baseline'"
    text = yaml.safe_dump(job)
    assert "secrets." not in text  # no model key in any step
    assert "DIP_SPEND_CAP_USD" not in text
    assert job["services"]["postgres"]["image"] == "postgres:17"
    runs = [s.get("run", "") for s in job["steps"]]
    assert any(r.strip() == "python scripts/live_eval.py baseline" for r in runs)
    assert any("tests/integration/test_live_eval.py" in r for r in runs)
    assert any("check_recordings.py agents/evals/baseline.json" in r for r in runs)
    check = next(r for r in runs if "test_live_eval.py" in r)
    assert check.strip().endswith("! grep -q FAILED artifacts/commit/checks.md")
    assert "permissions" not in job  # the workflow's contents: read, nothing more


def test_commit_results_takes_only_the_baseline_from_it():
    job = wf()["jobs"]["commit-results"]
    assert "live-baseline" in job["needs"]
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'elif [ "$BASELINE" = success ]; then name=live-baseline' in script
    m = re.search(r"allowed='([^']+)'", script)
    assert m
    allowed = re.compile(m.group(1))
    assert allowed.fullmatch("agents/evals/baseline.json")
    for bad in ("agents/evals/baseline.jsonx", "agents/evals/other.json",
                ".github/workflows/proof.yml", "agents/prompts/incident.md"):  # fmt: skip
        assert not allowed.fullmatch(bad), bad
    assert "live-baseline must bring exactly agents/evals/baseline.json" in script
    # only these two artifacts skip the scrub; recordings never do
    at = script.index('grep -qx "scrub: ok"')  # the scrub requirement is still there
    statement = script[script.rindex("\n#", 0, at):at]  # from its comment line to the grep
    assert sorted(re.findall(r'"\$ARTIFACT" = ([a-z-]+)', statement)) == [
        "live-baseline", "proof-commit"]  # fmt: skip
