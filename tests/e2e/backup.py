"""Stage 14 (14.1): one warehouse backup through the deployed stack.

  1. `python -m dip backup now` runs the warehouse-backup CronJob as a Job, now, and waits;
  2. its ops.backup row is ok, with the object's key, size and SHA-256;
  3. the object is in the backups bucket, non-empty, of that size and hash, and a pg_dump
     custom archive (magic PGDMP);
  4. `pg_restore --list` (in the Postgres pod, the server's own version) reads it back and
     lists the gold eligibility table's data and ops.backup itself;
  5. the bucket holds no more than the 7 dumps retention keeps;
  6. with the observability profile, Prometheus sees the backup
     (dip_backup_last_success_timestamp_seconds) and BackupMissing is not firing.

Env: KUBECONFIG (the kind cluster's, context kind-dip), WAREHOUSE_DSN, MINIO_URL,
MINIO_USER, MINIO_PASSWORD, PROMETHEUS_URL (empty without the observability profile).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUCKET = "backups"
KEEP = 7


def dip(*args: str) -> str:
    out = subprocess.run([sys.executable, "-m", "dip", *args], capture_output=True, text=True,
                         timeout=900, cwd=REPO, env={**os.environ, "PYTHONUTF8": "1"})  # fmt: skip
    print(out.stdout.strip())
    if out.returncode:
        raise RuntimeError(f"dip {' '.join(args)}: {out.stderr.strip()[-500:]}")
    return out.stdout.strip()


def latest_backup() -> tuple | None:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select backup_id, status, object_key, bytes, sha256, error,"
                    " extract(epoch from finished_at) from ops.backup"
                    " order by backup_id desc limit 1")  # fmt: skip
        return cur.fetchone()


def s3():
    import boto3

    return boto3.client("s3", endpoint_url=os.environ["MINIO_URL"],
                        aws_access_key_id=os.environ["MINIO_USER"],
                        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
                        region_name="us-east-1")  # fmt: skip


def restore_list(dump: bytes) -> str:
    # Into a file in the pod first: `pg_restore --list` reading a custom archive from stdin
    # over `kubectl exec -i` never returned (the first CI run timed out after 300 s).
    script = ("f=/tmp/e2e-backup.dump; cat > $f && pg_restore --list $f; rc=$?;"
              " rm -f $f; exit $rc")  # fmt: skip
    out = subprocess.run(["kubectl", "-n", "dip-data", "exec", "-i", "postgres-0", "--",
                          "sh", "-c", script], input=dump, capture_output=True,
                         timeout=300)  # fmt: skip
    if out.returncode:
        raise RuntimeError(f"pg_restore --list: {out.stderr.decode(errors='replace')[-500:]}")
    return out.stdout.decode("utf-8", errors="replace")


def prom_query(prom: str, query: str) -> list:
    q = urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(f"{prom}/api/v1/query?{q}", timeout=10) as r:
        return json.loads(r.read())["data"]["result"]


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    before = latest_backup()
    dip("backup", "now")
    row = latest_backup()
    check(row is not None and row != before, "the run recorded a new ops.backup row")
    if row is None:
        print("FAIL: no ops.backup row", file=sys.stderr)
        return 1
    backup_id, status, key, size, sha, error, finished = row
    check(status == "ok" and not error, f"backup {backup_id} is ok: {status} {error or ''}")
    check(bool(key) and bool(size) and bool(sha), f"the row names its dump: {key}, {size} B")
    if key:
        body = s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()
        check(len(body) > 0 and len(body) == size, f"the object is {len(body)} B, as recorded")
        check(hashlib.sha256(body).hexdigest() == sha, "its SHA-256 matches the row")
        check(body[:5] == b"PGDMP", "it is a pg_dump custom-format archive")
        listing = restore_list(body)
        check("TABLE DATA gold member_eligibility" in listing,
              "pg_restore --list reads it and lists gold.member_eligibility's data")  # fmt: skip
        check("TABLE ops backup" in listing, "the archive has ops.backup")
    keys = [o["Key"] for o in s3().list_objects_v2(Bucket=BUCKET, Prefix="warehouse/")
            .get("Contents", [])]  # fmt: skip
    check(0 < len(keys) <= KEEP, f"the bucket keeps at most {KEEP} dumps: {len(keys)}")
    prom = os.environ.get("PROMETHEUS_URL", "")
    if prom and finished:
        seen, end = [], time.monotonic() + 180  # a scrape every 30 s
        while time.monotonic() < end:
            seen = prom_query(prom, "dip_backup_last_success_timestamp_seconds")
            if seen and float(seen[0]["value"][1]) >= float(finished) - 1:
                break
            time.sleep(10)
        got = float(seen[0]["value"][1]) if seen else 0.0
        check(got >= float(finished) - 1, "Prometheus sees the backup's finish time")
        firing = prom_query(prom, 'ALERTS{alertname="BackupMissing",alertstate="firing"}')
        check(not firing, "BackupMissing is not firing")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: dip backup now wrote a dump to s3://backups/ that pg_restore reads back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
