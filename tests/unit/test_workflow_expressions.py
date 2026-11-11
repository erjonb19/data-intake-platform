"""24.25: no `${{ }}` expression inside a workflow's `run:` script. GitHub pastes an
expression's value into the script before the shell runs it, so a value an attacker can
shape (a branch name, a PR title, an actor) becomes code; through `env:` it stays data."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_no_expression_is_inlined_into_a_run_script():
    bad = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            for i, step in enumerate(job.get("steps") or []):
                if "${{" in str(step.get("run", "")):
                    bad.append(f"{wf.name}: {job_name} step {i} ({step.get('name', '')})")
    assert bad == [], "expressions inlined into run: scripts:\n" + "\n".join(bad)
