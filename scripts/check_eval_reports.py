"""Stage 12 (12.1, #83): the nightly evals' last check. The replay step writes the name of
every eval it starts to `<reports>/expected.txt`, one per line; this check passes only when
each of those evals wrote `<reports>/<name>/report.json`, no report is there that was not
started, and every case in every report passed.

It replaces a hard-coded report count, which went stale when S11.5 added a seventh eval and
failed the nightly run with every case passing (runs 41631832492, 41827356431,
42027130487).

    python scripts/check_eval_reports.py reports
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def summary(report: Path) -> tuple[object, object]:
    s = json.loads(report.read_text("utf-8"))["summary"]
    return s.get("cases_passed", s.get("passed")), s.get("cases", s.get("scenarios"))


def problems(reports: Path) -> list[str]:
    listing = reports / "expected.txt"
    if not listing.is_file():
        return [f"{listing} is missing: the replay step lists every eval it starts there"]
    expected = [n.strip() for n in listing.read_text("utf-8").splitlines() if n.strip()]
    if not expected:
        return [f"{listing} names no evals"]
    found = {p.parent.name: p for p in reports.glob("*/report.json")}
    out = [f"{n}: started, but wrote no report" for n in expected if n not in found]
    stray = sorted(set(found) - set(expected))
    out += [f"{n}: a report that no eval in {listing.name} started" for n in stray]
    for name in expected:
        if name in found:
            try:
                passed, total = summary(found[name])
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                out.append(f"{name}: unreadable report ({type(e).__name__}: {e})")
                continue
            if total in (None, 0) or passed != total:
                out.append(f"{name}: {passed}/{total} cases passed")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_eval_reports.py <reports dir>", file=sys.stderr)
        return 2
    reports = Path(args[0])
    bad = problems(reports)
    if bad:
        for line in bad:
            print(f"::error::{line}")
        return 1
    n = len(set((reports / "expected.txt").read_text("utf-8").split()))
    print(f"all {n} evals wrote a report and passed every case")
    return 0


if __name__ == "__main__":
    sys.exit(main())
