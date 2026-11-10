"""Stage 14 (14.2): the restore drill through the deployed stack, after tests/e2e/backup.py
left a fresh dump in s3://backups/.

  1. a count and an order-independent checksum of gold.member_eligibility, taken here,
     independently of the drill;
  2. `python -m dip drill drop_table`: the table is gone and ops.restore_drill holds a
     snapshot that matches step 1;
  3. `python -m dip restore --latest --verify`: it passes, warehouse_restore exists next to
     the live warehouse, and the table is back with the same count, checksum and grants;
  4. the drill's row says verified, with the backup and the time to restore, which goes in
     the job summary (GITHUB_STEP_SUMMARY).

Env: KUBECONFIG (context kind-dip), WAREHOUSE_DSN, MINIO_USER, MINIO_PASSWORD.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TABLE = "gold.member_eligibility"
SESSION = ("set timezone = 'UTC'; set datestyle = 'ISO, YMD'; set intervalstyle = 'postgres';"
           " set extra_float_digits = 1; set bytea_output = 'hex'")  # fmt: skip


def dip(*args: str) -> tuple[int, str]:
    out = subprocess.run([sys.executable, "-m", "dip", *args], capture_output=True, text=True,
                         timeout=900, cwd=REPO, env={**os.environ, "PYTHONUTF8": "1"})  # fmt: skip
    print(out.stdout.strip())
    if out.stderr.strip():
        print(out.stderr.strip()[-1500:], file=sys.stderr)
    return out.returncode, out.stdout


def query(sql: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"], autocommit=True) as conn:
        for stmt in SESSION.split("; "):
            conn.execute(stmt)
        return conn.execute(sql).fetchall()


def state() -> tuple | None:
    """Count, checksum and grants, computed here, not by the code under test."""
    if query(f"select to_regclass('{TABLE}')")[0][0] is None:
        return None
    (n, checksum), = query(f"select count(*), md5(string_agg(md5(t::text), ''"
                           f" order by md5(t::text))) from {TABLE} t")  # fmt: skip
    (acl,) = query(f"select relacl::text from pg_class where oid = '{TABLE}'::regclass")[0]
    return n, checksum, acl


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    before = state()
    check(before is not None and before[0] > 0, f"{TABLE} has rows before the drill: {before}")
    if before is None:
        print(f"FAIL: no {TABLE} to drill on", file=sys.stderr)
        return 1

    rc, _ = dip("drill", "drop_table")
    check(rc == 0, "dip drill drop_table ran")
    check(state() is None, f"{TABLE} is dropped")
    snap = query("select drill_id, row_count, checksum, coalesce(acl, ''), backup_id"
                 " from ops.restore_drill where restored_at is null"
                 " order by drill_id desc limit 1")  # fmt: skip
    check(len(snap) == 1, "ops.restore_drill holds a snapshot waiting for its restore")
    if snap:
        drill_id, rows, checksum, acl, backup_id = snap[0]
        check((rows, checksum, acl) == (before[0], before[1], before[2] or ""),
              "the snapshot matches the count, checksum and grants taken here")  # fmt: skip
        check(backup_id is not None, f"the snapshot names the newest good backup: {backup_id}")

    rc, out = dip("restore", "--latest", "--verify")
    check(rc == 0 and "PASS" in out, "dip restore --latest --verify passed")
    copies = query("select count(*) from pg_database where datname = 'warehouse_restore'")
    check(copies == [(1,)], "the dump was restored into warehouse_restore, not the live one")
    after = state()
    check(after == before, f"{TABLE} is back, same count, checksum and grants: {after}")

    done = query("select drill_id, verified, restore_backup_id, restore_seconds, detail"
                 " from ops.restore_drill order by drill_id desc limit 1")  # fmt: skip
    seconds = None
    if done:
        _, verified, restore_backup, seconds, detail = done[0]
        check(verified is True, f"the drill's row is verified: {detail}")
        what = f"it records backup {restore_backup} and the time to restore, {seconds} s"
        check(restore_backup is not None and seconds is not None, what)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and seconds is not None:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"restore drill (14.2): {TABLE}, {before[0]} rows, back from backup"
                    f" {done[0][2]} in {seconds} s"
                    f" ({'verified' if not failures else 'FAILED'})\n")  # fmt: skip

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"PASS: {TABLE} dropped, restored from the newest backup in {seconds} s, and it"
          " matches the snapshot")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
