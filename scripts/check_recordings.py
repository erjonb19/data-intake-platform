"""Check files made on a runner before they are committed (Stage 11): recordings, and a
proof's results. Run by the proof workflow's recording and proof jobs, which hold the model
key, and again by its commit-results job, which does not.

    python -I scripts/check_recordings.py [--key-env NAME]... PATH...

PATH is a file or a directory (its files, recursively). Fails (exit 1), naming the file and
the reason, on:

  - the value of any --key-env variable (the job's own key), anywhere;
  - an authentication header with a value that is not "[redacted]" (x-api-key,
    x-goog-api-key, authorization, as JSON or as `name: value`);
  - anything shaped like an Anthropic or Google API key, or a long bearer token, so the
    commit job, which has no key to compare with, still catches one.

Prints one line per check, which the jobs keep in checks.md for the commit message.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HEADER = re.compile(
    r"""["']?(x-api-key|x-goog-api-key|authorization)["']?\s*[:=]\s*["']?(?!\[redacted\])"""
    r"""([^"'\s,}]{4,})""",
    re.I,
)
SHAPES = {
    "an Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "a Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "a bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{24,}=*"),
}


def files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
    return out


def check(paths: list[str], keys: dict[str, str]) -> list[tuple[str, str]]:
    """(kind, problem) pairs: kind is "key" or "header"."""
    problems = []
    for f in files(paths):
        text = f.read_bytes().decode("utf-8", errors="replace")
        for name, value in keys.items():
            if value and value in text:
                problems.append(("key", f"{f}: holds the value of {name}"))
        for m in HEADER.finditer(text):
            problems.append(("header", f"{f}: an unredacted {m.group(1)} header"))
        for what, rx in SHAPES.items():
            if rx.search(text):
                problems.append(("key", f"{f}: something shaped like {what}"))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-env", action="append", default=[])
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    keys = {name: os.environ.get(name, "") for name in args.key_env}
    missing = [n for n, v in keys.items() if not v]
    if missing:
        print(f"key check: FAILED ({', '.join(missing)} not set: nothing to compare with)")
        return 1
    found = files(args.paths)
    if not found:
        print("key check: FAILED (no files to check)")
        return 1
    problems = check(args.paths, keys)
    for _, p in problems:
        print(f"  {p}", file=sys.stderr)
    bad = {kind for kind, _ in problems}
    who = f"{', '.join(keys)} and key shapes" if keys else "key shapes"
    print(f"key check ({who}): {'FAILED' if 'key' in bad else 'ok'}, {len(found)} files")
    print(f"auth-header check: {'FAILED' if 'header' in bad else 'ok'}, {len(found)} files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
