"""24.4: every Python install reads a hashed lock.

Each `pip install` in the workflows and the Dockerfiles either installs a lock from
requirements/ with --require-hashes --no-deps, or installs dip itself (a local path) with
--no-deps; and every lock pins every package with its hashes. Whether the locks match a
fresh compile is CI's lint job (`python scripts/lock.py --check`), which needs the network."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lock  # noqa: E402

LOCAL = re.compile(r"(/tmp/dip|/tmp/whl/\*\.whl|-e \.)(\s|$)")


def install_commands() -> list[tuple[str, str]]:
    files = [
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / "images").glob("*/Dockerfile"),
    ]
    out = []
    for f in files:
        text = f.read_text(encoding="utf-8").replace("\\\n", " ")
        for line in text.splitlines():
            for cmd in re.split(r"&&|;", line):
                if re.search(r"\bpip install\b", cmd) and not cmd.strip().startswith("#"):
                    out.append((f.relative_to(ROOT).as_posix(), cmd.strip()))
    return out


def test_every_pip_install_is_locked_or_local() -> None:
    bad = []
    for where, cmd in install_commands():
        locked = (
            "--require-hashes" in cmd
            and "--no-deps" in cmd
            and "requirements/" in cmd
            or (
                "--require-hashes" in cmd
                and "--no-deps" in cmd
                and re.search(r"-r /tmp/\S+\.txt", cmd)
            )
        )
        local = "--no-deps" in cmd and LOCAL.search(cmd) and "-r " not in cmd
        if not (locked or local):
            bad.append(f"{where}: {cmd}")
    assert bad == [], "unlocked pip installs:\n" + "\n".join(bad)


def test_there_are_installs_to_check() -> None:
    assert len(install_commands()) >= 15


def test_every_lock_exists_and_pins_with_hashes() -> None:
    for name in lock.LOCKS:
        text = (ROOT / "requirements" / name).read_text(encoding="utf-8")
        entries = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n", 2)[2])
        pins = [e for e in entries if e.strip() and not e.startswith("#")]
        assert pins, name
        for e in pins:
            assert re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?==\S+", e), (
                f"{name}: not pinned: {e[:60]}"
            )
            assert "--hash=sha256:" in e, f"{name}: no hash: {e[:60]}"


def test_the_cutoff_is_a_timestamp() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", lock.cutoff())
