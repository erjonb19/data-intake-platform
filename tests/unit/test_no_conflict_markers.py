"""No tracked file carries git conflict markers. A merge resolved in the code but not in the
docs once left `<<<<<<<` lines in docs/PROGRESS.md and README.md on a local branch
(2026-11-12); nothing else would have failed on them."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"^(<{7}|>{7})( |$)|^={7}$", re.M)


def test_no_tracked_file_has_conflict_markers():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()  # fmt: skip
    bad = []
    for name in files:
        path = ROOT / name
        if (
            path.suffix in {".png", ".pdf", ".xlsx", ".gz", ".zip", ".tgz", ".jar"}
            or not path.is_file()
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if MARKER.search(text):
            bad.append(name)
    assert bad == [], f"conflict markers in: {bad}"
