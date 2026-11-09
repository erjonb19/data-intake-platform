"""Warehouse backups (Stage 14, 14.1): a nightly `pg_dump` of the warehouse into the MinIO
bucket `backups`, the newest 7 kept, one `ops.backup` row per run.

The `warehouse-backup` CronJob (infra/terraform/backups.tf) runs it in two steps, in one
pod that shares an emptyDir:

  1. an init container from the Postgres image (pg_dump matches the server) writes
     `warehouse.dump` (custom format), checks it with `pg_restore --list`, and leaves
     `status` (ok or failed), `error` (pg_dump's stderr) and `started_at`; it always exits
     0, so a failed dump is still recorded;
  2. `python -m dip.backup` (the Airflow image: boto3 and psycopg2) records the run in
     ops.backup, uploads the dump to `backups/warehouse/warehouse-<UTC time>.dump`, checks
     the stored size, deletes all but the newest 7 dumps and marks the row ok (or failed,
     with the error).

    python -m dip backup now

starts one run now, as a Job made from the CronJob, and waits for it: only against the kind
cluster `kind-dip`, like the drills (dip/drills.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

BUCKET = "backups"
PREFIX = "warehouse/"
KEEP = 7
# The dumps this job writes, and so the only objects its retention may delete.
DUMP_KEY = re.compile(r"^warehouse/warehouse-\d{8}T\d{6}Z\.dump$")
NS = "dip-data"
CRONJOB = "warehouse-backup"


def dump_key(started: datetime) -> str:
    return f"{PREFIX}warehouse-{started.astimezone(UTC):%Y%m%dT%H%M%SZ}.dump"


def to_prune(keys: Iterable[str], keep: int = KEEP) -> list[str]:
    """The dumps past the newest `keep`, oldest first. Names sort by time (UTC, fixed
    width), so the order of the names is the order of the dumps. Anything else in the
    bucket is never touched."""
    if keep < 1:
        raise ValueError("keep at least one dump")
    dumps = sorted(k for k in keys if DUMP_KEY.match(k))
    return dumps[:-keep]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Backups(Protocol):
    def upload(self, path: Path, key: str, metadata: dict[str, str]) -> None: ...
    def sizes(self) -> dict[str, int]: ...
    def delete(self, key: str) -> None: ...


class S3Backups:
    """The backups bucket through boto3. upload_file streams the file (multipart past
    8 MB), so a large dump is never held in memory."""

    def __init__(self, client: Any, bucket: str = BUCKET):
        self.client, self.bucket = client, bucket

    def upload(self, path: Path, key: str, metadata: dict[str, str]) -> None:
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={"Metadata": metadata})

    def sizes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=PREFIX
        )
        for page in pages:
            out.update({o["Key"]: int(o["Size"]) for o in page.get("Contents", [])})
        return out

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def _read(path: Path, default: str = "") -> str:
    return path.read_text("utf-8", errors="replace").strip() if path.is_file() else default


def run(conn, store: Backups, work: Path, keep: int = KEEP,
        now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict:  # fmt: skip
    """Record, upload and prune one dump the init container left in `work`. Returns what
    happened; raises nothing (a failure is the row's status and error)."""
    started_raw = _read(work / "started_at")
    try:
        started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
    except ValueError:
        started = now()
    cur = conn.cursor()
    cur.execute("insert into ops.backup (started_at, status) values (%s, 'running')"
                " returning backup_id", (started,))  # fmt: skip
    backup_id = cur.fetchone()[0]
    conn.commit()

    def finish(status: str, error: str | None = None, **done: Any) -> dict:
        cur.execute(
            "update ops.backup set finished_at = %s, status = %s, error = %s,"
            " object_key = %s, bytes = %s, sha256 = %s where backup_id = %s",
            (now(), status, error[-1000:] if error else None, done.get("object_key"),
             done.get("bytes"), done.get("sha256"), backup_id),
        )  # fmt: skip
        conn.commit()
        return {"backup_id": backup_id, "status": status, "error": error, **done}

    if _read(work / "status") != "ok":
        return finish("failed", "pg_dump: " + (_read(work / "error") or "no dump was written"))
    dump = work / "warehouse.dump"
    try:
        size = dump.stat().st_size
        if size == 0:
            return finish("failed", "pg_dump wrote an empty file")
        sha = sha256_of(dump)
        key = dump_key(started)
        store.upload(dump, key, {"sha256": sha})
        stored = store.sizes().get(key)
        if stored != size:
            return finish("failed", f"stored {stored} bytes of {size}: {key}")
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        return finish("failed", f"upload: {type(exc).__name__}: {exc}")
    done = {"object_key": key, "bytes": size, "sha256": sha}
    try:
        pruned = to_prune(store.sizes(), keep)
        for old in pruned:
            store.delete(old)
    except Exception as exc:  # noqa: BLE001 - the dump is good; retention is not
        out = finish("ok", f"retention: {type(exc).__name__}: {exc}", **done)
        return {**out, "pruned": []}
    return {**finish("ok", **done), "pruned": pruned}


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError:  # the Airflow image ships psycopg2
        import psycopg2 as psycopg  # type: ignore[no-redef]
    return psycopg.connect(dsn, connect_timeout=10)


def main(env: dict[str, str] | None = None) -> int:
    """In the CronJob's pod: DIP_WAREHOUSE_DSN (dip_backup), DIP_S3_ENDPOINT,
    DIP_S3_ACCESS_KEY and DIP_S3_SECRET_KEY (the backup's MinIO user), DIP_BACKUP_DIR (the
    emptyDir), DIP_BACKUP_KEEP (7)."""
    import boto3

    env = dict(os.environ if env is None else env)
    client = boto3.client("s3", endpoint_url=env["DIP_S3_ENDPOINT"],
                          aws_access_key_id=env["DIP_S3_ACCESS_KEY"],
                          aws_secret_access_key=env["DIP_S3_SECRET_KEY"],
                          region_name="us-east-1")  # fmt: skip
    conn = _connect(env["DIP_WAREHOUSE_DSN"])
    try:
        out = run(conn, S3Backups(client, env.get("DIP_BACKUP_BUCKET", BUCKET)),
                  Path(env.get("DIP_BACKUP_DIR", "/backup")),
                  keep=int(env.get("DIP_BACKUP_KEEP", KEEP)))  # fmt: skip
    finally:
        conn.close()
    # numbers, the key and the error only: nothing here carries a credential
    print(json.dumps(out, default=str), flush=True)
    return 0 if out["status"] == "ok" and not out.get("error") else 1


# `dip backup now`: a Job from the CronJob, as the platform-drill E2E makes its check Job
# (tests/e2e/platform_drill.py, create_check_job).


def job_manifest(name: str, rendered: str, ttl: int = 900) -> str:
    """The Job `kubectl create job --from=cronjob/warehouse-backup` would make, without its
    owner reference: owned by the CronJob, it would count toward the CronJob's history
    limit and could be deleted by the controller before it is read (#103). A TTL cleans it
    up instead, after the caller has read it."""
    job = json.loads(rendered)
    job["spec"]["ttlSecondsAfterFinished"] = ttl
    job["metadata"].pop("ownerReferences", None)
    job["metadata"]["name"] = name
    job["metadata"].setdefault("labels", {})["dip-backup"] = "manual"
    return json.dumps(job)


def _job_state(kubectl: Callable[..., str], name: str) -> str:
    got = kubectl("-n", NS, "get", "job", name, "-o",
                  "jsonpath={.status.succeeded}/{.status.failed}")  # fmt: skip
    ok, failed = (got.split("/") + ["", ""])[:2]
    return "ok" if ok and ok != "0" else "failed" if failed and failed != "0" else ""


def _kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=300,
                         input=stdin)  # fmt: skip
    if check and out.returncode:
        from dip.drills import DrillError

        raise DrillError(f"kubectl {' '.join(args[:4])}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def now_job(kubectl: Callable[..., str] = _kubectl, timeout_s: int = 600,
            sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:  # fmt: skip
    """Start a backup now and wait for it: (job name, "ok" | "failed" | "timeout")."""
    from dip.drills import require_kind

    require_kind(kubectl)
    name = f"{CRONJOB}-now-{int(time.time())}"
    rendered = kubectl("-n", NS, "create", "job", name, f"--from=cronjob/{CRONJOB}",
                       "--dry-run=client", "-o", "json")  # fmt: skip
    kubectl("-n", NS, "create", "-f", "-", stdin=job_manifest(name, rendered))
    state, end = "", time.monotonic() + timeout_s
    while not state and time.monotonic() < end:
        sleep(3)
        state = _job_state(kubectl, name)
    return name, state or "timeout"


def now(kubectl: Callable[..., str] = _kubectl) -> int:
    name, state = now_job(kubectl)
    logs = kubectl("-n", NS, "logs", f"job/{name}", "-c", "upload", "--tail=20", check=False)
    print(f"job {name}: {state}")
    if logs:
        print(logs)
    return 0 if state == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
