"""12.10: the release notes are the docs/PROGRESS.md "Finished items" rows added since the
previous tag, and the release workflow publishes only on a tag push."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release_notes", ROOT / "scripts" / "release_notes.py"
)
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)

BEFORE = """# Progress

## Model spend

| Month | Spend |
|---|---|
| 2026-10 | $1 |

## Finished items

| Date | Item | What changed | How to check it |
|---|---|---|---|
| 2026-11-06 | 12.1 Nightly evals | the count | `gh workflow run evals.yml` |
"""
NOW = BEFORE + "| 2026-11-08 | 12.8 PDF bomb | a budget | `pytest tests/unit/test_pdffeed.py` |\n"


def test_only_rows_added_since_the_previous_tag():
    notes = rn.notes("v0.13.0", "v0.12.0", NOW, BEFORE)
    assert "1 finished item(s) since v0.12.0" in notes
    assert "## 12.8 PDF bomb (2026-11-08)" in notes
    assert "**Check:** `pytest tests/unit/test_pdffeed.py`" in notes
    assert "12.1" not in notes


def test_the_first_release_has_every_row_and_not_the_spend_table():
    notes = rn.notes("v0.12.0", None, NOW, "")
    assert "2 finished item(s) in the first release" in notes
    assert "2026-10" not in notes


def test_the_real_progress_file_parses():
    rows = rn.finished_rows((ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8"))
    assert rows and all(len(rn.cells(r)) == 4 for r in rows)


def test_the_new_tag_is_not_its_own_previous_tag(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a")
    git("tag", "v0.1.0")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b")
    git("tag", "v0.2.0")
    out = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-v:refname", "--merged", "v0.2.0"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    assert [t for t in out if t != "v0.2.0"][0] == "v0.1.0"


def test_the_workflow_publishes_only_on_a_tag_push():
    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the key `on` as True
    assert on["push"] == {"tags": ["v*"]}
    steps = {s.get("name"): s for s in wf["jobs"]["release"]["steps"]}
    # A manual run started on a tag also has GITHUB_REF_TYPE=tag: only a push may publish.
    for publishing in ("Push the images", "The GitHub release"):
        assert steps[publishing]["if"].startswith("github.event_name == 'push' && ")
    version = steps["Version"]["run"]
    assert '[ "${GITHUB_EVENT_NAME}" = push ] && [ "${GITHUB_REF_TYPE}" = tag ]' in version
    assert "${{" not in steps["Release notes"]["run"]  # values come in through env
    built = steps["Build the images"]["run"]
    for image in ("dip-airflow", "dip-ocr", "dip-mcp", "dip-intake-api"):
        assert f"build {image} " in built
