"""24.5: every GitHub Action is pinned by its full commit SHA, with the version it was in a
comment, so a moved tag cannot run new code in a job that holds a key or a write token.
Dependabot's github-actions updates keep the SHA and the comment current."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def test_every_action_is_pinned_by_sha_with_its_version():
    bad = []
    seen = 0
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = USES.match(line)
            if not m or m.group(1).startswith("./"):
                continue
            seen += 1
            if not PINNED.match(m.group(1)) or not re.match(r"\s+# v\d+\.\d+\.\d+$", m.group(2)):
                bad.append(f"{wf.name}:{n}: {line.strip()}")
    assert seen >= 40
    assert bad == [], "actions not pinned by SHA with a # vX.Y.Z comment:\n" + "\n".join(bad)
