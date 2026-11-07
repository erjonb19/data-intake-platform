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
    missing = [
        code
        for item in checklist()
        for code in re.findall(r"`([^`]+)`", item)
        if "/" in code and " " not in code and not (ROOT / code.rstrip("/")).exists()
    ]
    assert missing == [], f"README checklist names paths that do not exist: {missing}"


BACKLOG = (ROOT / "docs" / "BACKLOG.md").read_text(encoding="utf-8")


def item_refs(item: str) -> list[str]:
    """The backlog items a line names: `(13.1)`, `(14.1, 14.2)`, `(Stage 15)`, `; 12.1)`."""
    refs = []
    for group in re.findall(r"[(;]\s*((?:Stage )?\d+(?:\.\d+)?(?:, \d+\.\d+)*)\)", item):
        refs += [r.strip() for r in group.split(",")]
    return refs


def ticked(ref: str) -> bool:
    """Whether a backlog item (`13.1`) or a whole stage (`Stage 15`) is done."""
    if ref.startswith("Stage "):
        stage = BACKLOG.split(f"## {ref}:", 1)
        assert len(stage) == 2, f"{ref} is not in docs/BACKLOG.md"
        body = stage[1].split("\n## ", 1)[0]
        return "- [ ] " not in body
    line = re.search(rf"^- \[([ x])\] \*\*{re.escape(ref)} ", BACKLOG, re.M)
    assert line, f"{ref} is not an item in docs/BACKLOG.md"
    return line.group(1) == "x"


def test_unbuilt_lines_name_an_open_backlog_item() -> None:
    for item in checklist():
        if item.startswith("- [ ]"):
            assert "not yet built" in item, item
            refs = item_refs(item)
            assert refs, f"no backlog item named: {item}"
            for ref in refs:
                assert not ticked(ref), f"{ref} is done in BACKLOG but unticked here: {item}"


def test_built_lines_name_only_done_items() -> None:
    for item in checklist():
        if item.startswith("- [x]"):
            for ref in item_refs(item):
                assert ticked(ref), f"{ref} is still open in BACKLOG: {item}"
