"""Stage 12 (12.3): the README's Production readiness checklist stays honest. Every path a
line names exists, every unbuilt line names the backlog item that builds it, and the README
says the platform runs on Kubernetes locally."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def checklist() -> list[str]:
    """The section's checklist lines, each joined with its continuation lines."""
    section = README.split("## Production readiness", 1)[1].split("\n## ", 1)[0]
    items: list[str] = []
    for line in section.splitlines():
        if line.startswith("- ["):
            items.append(line)
        elif items and line.startswith("  "):
            items[-1] += " " + line.strip()
        elif not line.strip():
            continue
        else:
            items.append("")  # a heading or paragraph ends the item
    return [i for i in items if i]


def test_says_runs_on_kubernetes_locally() -> None:
    assert "runs on Kubernetes locally" in README


def test_checklist_has_built_and_unbuilt_lines() -> None:
    items = checklist()
    assert any(i.startswith("- [x]") for i in items)
    assert any(i.startswith("- [ ]") for i in items)


def test_every_named_path_exists() -> None:
    missing = []
    for item in checklist():
        for code in re.findall(r"`([^`]+)`", item):
            if "/" in code and " " not in code and not code.startswith("ops."):
                if not (ROOT / code.rstrip("/")).exists():
                    missing.append(code)
    assert missing == [], f"README checklist names paths that do not exist: {missing}"


def test_every_unbuilt_line_names_its_backlog_item() -> None:
    backlog = (ROOT / "docs" / "BACKLOG.md").read_text(encoding="utf-8")
    for item in checklist():
        if not item.startswith("- [ ]"):
            continue
        assert "not yet built" in item, item
        refs = re.findall(r"\((?:Stage )?(\d+(?:\.\d+)?)(?:, (\d+\.\d+))?\)", item)
        assert refs, f"no backlog item named: {item}"
        for ref in (r for pair in refs for r in pair if r):
            label = f"**{ref} " if "." in ref else f"## Stage {ref}:"
            assert label in backlog, f"{ref} is not in docs/BACKLOG.md: {item}"
