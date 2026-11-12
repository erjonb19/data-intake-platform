"""The restore drill (Stage 14, 14.2): bring a dropped table back from the newest good
warehouse backup and prove it is the same table.

    python -m dip drill drop_table          # snapshot gold.member_eligibility, drop it
    python -m dip restore --latest --verify # restore it from the newest backup, compare

The drill (dip/drills.py) records the table's row count, order-independent checksum, owner
and grants in ops.restore_drill, in the transaction that drops it. The restore:

  1. reads the newest `ok` row of ops.backup;
  2. has the Postgres pod download that dump from MinIO inside the cluster, with a GET URL
     signed here as the MinIO admin (the backup's own MinIO user cannot read a dump back,
     by design), and checks its size and SHA-256 there against the row. Not piped in over
     `kubectl exec -i`: in the first CI run of this drill the 900 KB dump arrived with a
     different SHA-256, and in 14.1 pg_restore reading stdin that way never returned
     (docs/incidents.md, 2026-11-09);
  3. restores the whole dump into a new database, `warehouse_restore` (dropped and made
     again each time; never the live one), and compares the table there with the snapshot;
  4. brings the dropped table back into `warehouse` with `pg_restore -L`, a list of the
     dump's entries for that table alone (its definition, its data, its grants and
     comments), in one transaction. `--table` alone would leave the grants behind;
  5. compares the live table's count, checksum, owner and grants with the snapshot, prints
     the time to restore and records it on the drill's row.

Every step runs through kubectl against the Postgres pod, and only when the current
context is the kind cluster `kind-dip` (drills.require_kind). The URL is signed with
MINIO_USER and MINIO_PASSWORD (`terraform output -raw minio_root_user`,
`minio_root_password`).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

from dip.backup import BUCKET, DUMP_KEY
from dip.drills import DrillError, require_kind

NS = "dip-data"
POD = "postgres-0"
LIVE_DB = "warehouse"
RESTORE_DB = "warehouse_restore"
POD_DUMP = "/tmp/dip-restore.dump"
POD_LIST = "/tmp/dip-restore.list"
DRILL_TABLE = "gold.member_eligibility"
MINIO_IN_CLUSTER = f"http://minio.{NS}.svc.cluster.local:9000"  # infra/terraform/secrets.tf
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# The same text for every row, whatever the session: dates, times, intervals and floats
# printed one way, so a checksum taken before the drop and one taken after the restore
# compare the data and nothing else.
SESSION = ("set timezone = 'UTC'; set datestyle = 'ISO, YMD'; set intervalstyle = 'postgres';"
           " set extra_float_digits = 1; set bytea_output = 'hex'; ")  # fmt: skip


def _table(name: str) -> str:
    if not TABLE_NAME.match(name):
        raise DrillError(f"not a schema.table name: {name!r}")
    return name


def checksum_sql(table: str) -> str:
    """Row count and an order-independent checksum: the md5 of every row's text, sorted,
    joined and hashed again. Any changed, missing or extra row changes it."""
    t = _table(table)
    return (SESSION + "select count(*), coalesce(md5(string_agg(md5(t::text), ''"
            f" order by md5(t::text))), md5('')) from {t} t")  # fmt: skip


def acl_sql(table: str) -> str:
    t = _table(table)
    return (f"select pg_get_userbyid(relowner), coalesce(relacl::text, '') from pg_class"
            f" where oid = '{t}'::regclass")  # fmt: skip


LATEST_OK_SQL = ("select backup_id from ops.backup where status = 'ok'"
                 " order by finished_at desc nulls last, backup_id desc limit 1")  # fmt: skip


def precheck_sql(table: str = DRILL_TABLE) -> str:
    """For the drill: is the table there, is a drill waiting for its restore, and which is
    the newest good backup."""
    t = _table(table)
    return (f"select to_regclass('{t}') is not null,"
            " coalesce((select drill_id::text from ops.restore_drill where restored_at is null"
            " order by drill_id desc limit 1), ''),"
            f" coalesce(({LATEST_OK_SQL})::text, '')")  # fmt: skip


def drop_sql(table: str = DRILL_TABLE) -> str:
    """The snapshot and the drop, in one transaction: either both happen or neither. No
    CASCADE: if anything depends on the table, the drill fails instead of dropping more."""
    t = _table(table)
    return (SESSION + "begin; insert into ops.restore_drill"
            " (table_name, row_count, checksum, owner, acl, backup_id)"
            f" select '{t}', s.n, s.sum, pg_get_userbyid(c.relowner), c.relacl::text,"
            f" ({LATEST_OK_SQL})"
            " from (select count(*) as n, coalesce(md5(string_agg(md5(t::text), ''"
            f" order by md5(t::text))), md5('')) as sum from {t} t) s, pg_class c"
            f" where c.oid = '{t}'::regclass"
            " returning drill_id, row_count, checksum, coalesce(backup_id::text, '');"
            f" drop table {t}; commit;")  # fmt: skip


NEWEST_BACKUP_SQL = ("select backup_id, object_key, bytes, sha256 from ops.backup"
                     " where status = 'ok' order by finished_at desc nulls last,"
                     " backup_id desc limit 1")  # fmt: skip
# The backup a pending drill recorded when it dropped the table: a backup taken after the
# drop (the nightly CronJob, or `dip backup now`) no longer holds the table.
DRILL_BACKUP_SQL = ("select backup_id, object_key, bytes, sha256 from ops.backup"
                    " where status = 'ok' and backup_id = (select backup_id from"
                    " ops.restore_drill where restored_at is null order by drill_id desc"
                    " limit 1)")  # fmt: skip
PENDING_SQL = ("select drill_id, table_name, row_count, checksum, owner, coalesce(acl, '')"
               " from ops.restore_drill where restored_at is null"
               " order by drill_id desc limit 1")  # fmt: skip


def toc_entries(listing: str, table: str) -> list[str]:
    """The lines of `pg_restore --list` for one table: its definition and data, and the
    grants and comments on it and its columns. Lines read `<id>; <oid> <oid> <type>
    <schema> <name> <owner>`."""
    schema, name = _table(table).split(".")
    own = re.compile(rf"^(TABLE|TABLE DATA) {schema} {name}( |$)")
    deps = re.compile(rf"^(ACL|COMMENT) {schema} (TABLE {name}|COLUMN {name}\.\S+)( |$)")
    out = []
    for line in listing.splitlines():
        m = re.match(r"^\d+; \d+ \d+ (.+)$", line.strip())
        if m and (own.match(m.group(1)) or deps.match(m.group(1))):
            out.append(line.strip())
    kinds = {re.match(r"^\d+; \d+ \d+ (TABLE DATA|TABLE)", e).group(1)
             for e in out if re.match(r"^\d+; \d+ \d+ TABLE", e)}  # fmt: skip
    if kinds != {"TABLE", "TABLE DATA"}:
        raise DrillError(f"the dump has no table and data entries for {table}")
    return out


def kubectl(*args: str, check: bool = True, stdin: bytes | None = None,
            timeout: int = 900) -> str:  # fmt: skip
    """kubectl; stdin carries only short text (a URL, a list), never a dump."""
    out = subprocess.run(["kubectl", *args], input=stdin, capture_output=True,
                         timeout=timeout)  # fmt: skip
    if check and out.returncode:
        err = out.stderr.decode("utf-8", errors="replace").strip()
        raise DrillError(f"kubectl {' '.join(args[:6])}: {err[-400:]}")
    return out.stdout.decode("utf-8", errors="replace").strip()


def in_pod(run: Callable[..., str], *cmd: str, **kw: Any) -> str:
    """A command in the Postgres pod; `-i` only when it reads stdin."""
    tty = ["-i"] if kw.get("stdin") is not None else []
    return run("-n", NS, "exec", *tty, POD, "--", *cmd, **kw)


def psql(run: Callable[..., str], db: str, sql: str) -> str:
    return in_pod(run, "psql", "-X", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1",
                  "-Atq", "-c", sql)  # fmt: skip


def snapshot_of(run: Callable[..., str], db: str, table: str) -> tuple[str, str]:
    rows, checksum = psql(run, db, checksum_sql(table)).splitlines()[-1].split("|")
    return rows, checksum


def _s3() -> Any:
    """A client for signing only: its endpoint is MinIO's address inside the cluster, which
    the Postgres pod uses. Signing makes no request."""
    import boto3
    from botocore.config import Config

    missing = [v for v in ("MINIO_USER", "MINIO_PASSWORD") if not os.environ.get(v)]
    if missing:
        raise DrillError(f"set {' and '.join(missing)} (terraform output -raw"
                         " minio_root_user, minio_root_password)")  # fmt: skip
    return boto3.client("s3", endpoint_url=MINIO_IN_CLUSTER,
                        aws_access_key_id=os.environ["MINIO_USER"],
                        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
                        region_name="us-east-1",
                        config=Config(signature_version="s3v4",
                                      s3={"addressing_style": "path"}))  # fmt: skip


def restore_latest(verify: bool = False, run: Callable[..., str] = kubectl,
                   s3: Any = None, clock: Callable[[], float] = time.monotonic,
                   say: Callable[[str], None] = print) -> dict:  # fmt: skip
    """Restore the newest good dump into warehouse_restore and bring a dropped table back.
    Returns what happened; raises DrillError when a step cannot run."""
    if RESTORE_DB == LIVE_DB:  # never, but this is the one thing that must not happen
        raise DrillError("the restore database must not be the live one")
    require_kind(run)
    t0 = last = clock()
    steps: dict[str, float] = {}

    def mark(step: str) -> None:
        nonlocal last
        now = clock()
        steps[step], last = round(now - last, 1), now

    pending = psql(run, LIVE_DB, PENDING_SQL)
    if verify and not pending:
        raise DrillError("nothing to verify against: no drill is waiting for a restore"
                         " (run `python -m dip drill drop_table` first)")  # fmt: skip
    if pending:  # the dump the drill recorded, which still holds the table
        newest = psql(run, LIVE_DB, DRILL_BACKUP_SQL)
        if not newest:
            raise DrillError("the backup the pending drill recorded is not in ops.backup as ok")
    else:
        newest = psql(run, LIVE_DB, NEWEST_BACKUP_SQL)
    if not newest:
        raise DrillError("no good backup in ops.backup: run `python -m dip backup now` first")
    backup_id, key, size, sha = newest.splitlines()[-1].split("|")
    if not DUMP_KEY.match(key):
        raise DrillError(f"ops.backup {backup_id} names an unexpected object: {key!r}")
    say(f"backup {backup_id}: s3://{BUCKET}/{key} ({size} B)")

    # The pod fetches the dump itself, from MinIO inside the cluster, with a GET URL signed
    # here for that one object for 5 minutes. The URL goes to the pod over stdin, so it is
    # never in kubectl's arguments on this machine; inside the pod it is wget's argument for
    # the length of the download.
    url = (s3 or _s3()).generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=300)  # fmt: skip
    out: dict[str, Any] = {"backup_id": int(backup_id), "object_key": key}
    try:
        fetch = f'{{ read -r url || [ -n "$url" ]; }} && wget -q -O {POD_DUMP} "$url"'
        in_pod(run, "sh", "-c", fetch, stdin=(url + "\n").encode())
        got = in_pod(run, "sh", "-c", f"wc -c < {POD_DUMP} && sha256sum {POD_DUMP}").split()
        got_size, got_sha = (got[0], got[1]) if len(got) >= 2 else ("?", "?")
        if got_size != size or got_sha != sha:
            raise DrillError(f"the dump in the pod does not match ops.backup {backup_id}:"
                             f" {got_size} B, SHA-256 {got_sha}"
                             f" (the row: {size} B, {sha})")  # fmt: skip
        say(f"downloaded into {POD}, size and SHA-256 match the row: {sha}")
        mark("download into the pod")
        owner = psql(run, LIVE_DB, "select pg_get_userbyid(datdba) from pg_database"
                     " where datname = current_database()")  # fmt: skip
        in_pod(run, "dropdb", "-U", "postgres", "--if-exists", RESTORE_DB)
        in_pod(run, "createdb", "-U", "postgres", "-O", owner, RESTORE_DB)
        in_pod(run, "pg_restore", "-U", "postgres", "-d", RESTORE_DB, "--exit-on-error",
               POD_DUMP)  # fmt: skip
        say(f"restored the whole dump into {RESTORE_DB}")
        mark(f"restore into {RESTORE_DB}")
        if not pending:
            out.update(verified=None, seconds=round(clock() - t0, 1), steps=steps)
            say(f"no drill waiting: {RESTORE_DB} holds the backup; nothing brought back")
            return out
        drill_id, table, rows, checksum, towner, acl = pending.split("|", 5)
        table = _table(table)
        copy = snapshot_of(run, RESTORE_DB, table)
        say(f"{table} in {RESTORE_DB}: {copy[0]} rows, checksum {copy[1]}")
        there = psql(run, LIVE_DB, f"select to_regclass('{table}') is not null") == "t"
        if there:
            say(f"{table} is already in {LIVE_DB}: not brought back")
        else:
            entries = toc_entries(in_pod(run, "pg_restore", "--list", POD_DUMP), table)
            # As arguments, not stdin: `kubectl exec -i` sometimes loses a short stdin, and
            # `pg_restore -L` with an empty list restores nothing and still exits 0 (the
            # upgrade E2E's second drill failed that way three times, 2026-11-11). The
            # entries are lines of the dump's table of contents, nothing secret.
            in_pod(run, "sh", "-c", f'printf "%s\\n" "$@" > {POD_LIST}', "sh", *entries)
            written = in_pod(run, "sh", "-c", f"grep -c . {POD_LIST} || true").strip()
            if written != str(len(entries)):
                raise DrillError(f"the restore list in the pod has {written or 0} lines,"
                                 f" not {len(entries)}")  # fmt: skip
            in_pod(run, "pg_restore", "-U", "postgres", "-d", LIVE_DB, "--exit-on-error",
                   "--single-transaction", "-L", POD_LIST, POD_DUMP)  # fmt: skip
            if psql(run, LIVE_DB, f"select to_regclass('{table}') is not null") != "t":
                raise DrillError(f"pg_restore exited 0 but {table} is not in {LIVE_DB}")
            say(f"brought {table} back into {LIVE_DB} ({len(entries)} entries of the dump)")
        mark(f"back into {LIVE_DB}")
        live = snapshot_of(run, LIVE_DB, table)
        live_owner, live_acl = psql(run, LIVE_DB, acl_sql(table)).split("|", 1)
        mark("verify")
        seconds = round(clock() - t0, 1)
        checks = {
            f"rows in {RESTORE_DB}": copy[0] == rows,
            f"checksum in {RESTORE_DB}": copy[1] == checksum,
            f"rows in {LIVE_DB}": live[0] == rows,
            f"checksum in {LIVE_DB}": live[1] == checksum,
            "owner": live_owner == towner,
            "grants": live_acl == acl,
        }
        verified = all(checks.values())
        failed = [k for k, ok in checks.items() if not ok]
        detail = "matches the snapshot" if verified else "differs: " + ", ".join(failed)
        psql(run, LIVE_DB, f"update ops.restore_drill set restored_at = now(),"
             f" restore_backup_id = {int(backup_id)}, restore_seconds = {seconds},"
             f" verified = {'true' if verified else 'false'}, detail = '{detail}'"
             f" where drill_id = {int(drill_id)}")  # fmt: skip
        say(f"snapshot (drill {drill_id}): {rows} rows, checksum {checksum}")
        say(f"{table} in {LIVE_DB}: {live[0]} rows, checksum {live[1]}: {detail}")
        out.update(drill_id=int(drill_id), table=table, rows=int(live[0]), checksum=live[1],
                   verified=verified, failed=failed, seconds=seconds, steps=steps)  # fmt: skip
        return out
    finally:
        in_pod(run, "rm", "-f", POD_DUMP, POD_LIST, check=False)
        if "seconds" in out:
            took = ", ".join(f"{k} {v} s" for k, v in steps.items())
            say(f"time to restore: {out['seconds']} s ({took})")


def main(verify: bool, **kw: Any) -> int:
    out = restore_latest(verify, **kw)
    if verify and not out.get("verified"):
        print(f"FAILED: the restored table does not match the snapshot: {out.get('failed')}")
        return 1
    if verify:
        print("PASS: the restored table matches the snapshot taken before the drop")
    return 0
