"""Release notes from docs/PROGRESS.md (12.10).

The notes for a tag are the "Finished items" rows added since the previous tag: each row's
item and what changed, with how to check it. For the first tag, every row is new.

    python scripts/release_notes.py v0.12.0 [--previous v0.11.0] > notes.md

Without --previous, the newest earlier `v*` tag is used (none for the first release).
"""

from __future__ import annotations

import argparse
import subprocess
import sys

PROGRESS = "docs/PROGRESS.md"
SECTION = "## Finished items"


def finished_rows(text: str) -> list[str]:
    """The data rows of the "Finished items" table, in file order."""
    if SECTION not in text:
        return []
    table = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells and cells[0][:2] == "20":
            rows.append(line)
    return rows


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def notes(tag: str, previous: str | None, now: str, before: str) -> str:
    """Markdown notes: the rows in `now` that are not in `before`."""
    old = set(finished_rows(before))
    new = [r for r in finished_rows(now) if r not in old]
    since = f"since {previous}" if previous else "in the first release"
    lines = [f"# {tag}", "", f"{len(new)} finished item(s) {since}, from `{PROGRESS}`.", ""]
    for row in new:
        date, item, what, check = (cells(row) + ["", "", "", ""])[:4]
        lines += [f"## {item} ({date})", "", what, "", f"**Check:** {check}", ""]
    if not new:
        lines.append("No finished items were added.")
    return "\n".join(lines).rstrip() + "\n"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def previous_tag(tag: str) -> str | None:
    tags = _git("tag", "--list", "v*", "--sort=-v:refname", "--merged", tag).split()
    earlier = [t for t in tags if t != tag]
    return earlier[0] if earlier else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", help="the tag (or any ref) the release is for")
    ap.add_argument("--previous", help="the tag before it (default: the newest earlier v*)")
    args = ap.parse_args(argv)
    previous = args.previous or previous_tag(args.tag)
    now = _git("show", f"{args.tag}:{PROGRESS}")
    before = _git("show", f"{previous}:{PROGRESS}") if previous else ""
    sys.stdout.write(notes(args.tag, previous, now, before))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
