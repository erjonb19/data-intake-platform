"""Compile requirements/*.in into hashed lock files (24.4), or check that they are current.

Every Python install in CI, the proof jobs and the images reads one of these locks with
`pip install --require-hashes --no-deps`: exact versions and the SHA-256 of every file, for
the whole dependency tree. uv resolves them (`uv pip compile --universal --generate-hashes`)
for Python 3.12, the version CI and every image run, on every platform.

The cooldown: nothing uploaded after the cutoff in requirements/cutoff.txt is chosen
(`--exclude-newer`). `--refresh` moves the cutoff to 7 days ago before compiling, so a
release must have been public for a week before a lock can pick it up.

    python scripts/lock.py             compile every lock at the current cutoff
    python scripts/lock.py --refresh   move the cutoff to 7 days ago, then compile
    python scripts/lock.py --check     fail if any lock differs from a fresh compile (CI)

Needs uv (requirements/tools.txt pins it). Locks that install into the official Airflow
image's environment are resolved against Airflow's own constraints, so they never move a
package Airflow ships.
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements"
CUTOFF = REQ / "cutoff.txt"
AIRFLOW_CONSTRAINTS = "requirements/airflow-constraints-3.4.0-py3.12.txt"
PYTHON = "3.12"
COOLDOWN = timedelta(days=7)

# lock file -> (sources, extra uv arguments). Paths are relative to the repo root.
LOCKS: dict[str, tuple[list[str], list[str]]] = {
    "dev.txt": (["pyproject.toml", "requirements/build.in"], ["--extra", "dev"]),
    "tools.txt": (["requirements/tools.in"], []),
    "build.txt": (["requirements/build.in"], []),
    "e2e.txt": (["requirements/e2e.in"], []),
    "agent.txt": (["requirements/agent.in"], []),
    "dbt.txt": (["requirements/dbt.in"], []),
    "intake-api.txt": (["requirements/intake-api.in"], []),
    "mcp.txt": (["requirements/mcp.in"], []),
    "airflow-extra.txt": (["requirements/airflow-extra.in"], ["-c", AIRFLOW_CONSTRAINTS]),
    "ocr.txt": (["requirements/ocr.in"], ["-c", AIRFLOW_CONSTRAINTS]),
}


def cutoff() -> str:
    return CUTOFF.read_text(encoding="utf-8").strip()


def compile_lock(name: str, out: Path, uv: str) -> None:
    sources, extra = LOCKS[name]
    cmd = [uv, "pip", "compile", *sources, *extra, "--universal", "--python-version", PYTHON,
           "--generate-hashes", "--exclude-newer", cutoff(), "--quiet",
           "--custom-compile-command", "python scripts/lock.py",
           "--output-file", str(out)]  # fmt: skip
    subprocess.run(cmd, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if a lock is stale")
    mode.add_argument("--refresh", action="store_true", help="cutoff = 7 days ago, compile")
    p.add_argument("--uv", default="uv", help="the uv executable")
    args = p.parse_args(argv)

    if args.refresh:
        when = (datetime.now(UTC) - COOLDOWN).replace(hour=0, minute=0, second=0, microsecond=0)
        CUTOFF.write_text(when.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8")
    if not args.check:
        for name in LOCKS:
            compile_lock(name, REQ / name, args.uv)
            print(f"wrote requirements/{name}")
        return 0

    stale = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in LOCKS:
            fresh = Path(tmp) / name
            compile_lock(name, fresh, args.uv)
            want = fresh.read_text(encoding="utf-8").splitlines(keepends=True)
            path = REQ / name
            have = (
                path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
            )
            if have != want:
                stale.append(name)
                sys.stdout.writelines(difflib.unified_diff(have, want, f"requirements/{name}",
                                                           "fresh compile", n=1))  # fmt: skip
    if stale:
        print(f"stale locks: {', '.join(stale)}; run `python scripts/lock.py` and commit",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"every lock is current (cutoff {cutoff()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
