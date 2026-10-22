"""Stage 1+ end-to-end cases (PLAN.md section 6), from tests/e2e/cases.yaml.

For each feed: drop its fixtures into `landing/<feed>/`, trigger the feed's intake DAG,
wait for it, then check every case in the warehouse:
  - the file's ops.intake_file status and its set of quarantine reason codes;
  - a file quarantined whole has a file-level quarantine row and nothing of it is in
    bronze, silver or gold;
  - otherwise its rows in gold: the expected count, and a checksum over every contract
    column computed independently from the fixture (minus `exclude_rows`).

Cases with `channel: sftp` are uploaded to the feed's vendor account over SFTP instead,
and the `sftp_sync` DAG moves them to landing first (Stage 2); their delivery must be
recorded with channel sftp.

Safe to rerun: processed files are recognized by sha256.
Env: as smoke.py, plus MINIO_USER, MINIO_PASSWORD and WAREHOUSE_DSN; for SFTP cases also
SFTP_PORT (a port-forward to the server), SFTP_HOST_KEY and SFTP_KEY_DIR (one
<account>.key per vendor account), which scripts/e2e.sh sets up.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
EMPTY = (0, hashlib.md5(b"").hexdigest())


def load_cases() -> dict:
    return yaml.safe_load((HERE / "cases.yaml").read_text("utf-8"))


def dag_id_for(feed: str) -> str:
    # Same rule as dip.contracts.dag_id_for (a unit test keeps them equal); the E2E venv
    # does not install the dip package.
    return "intake__" + feed.replace(".", "__")


def fixture_checksum(data: bytes, exclude: set[int], columns: list[str]) -> tuple[int, str]:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    kept = [r for n, r in enumerate(rows, start=1) if n not in exclude]
    lines = sorted("|".join(r[c].strip() for c in columns) for r in kept)
    return len(kept), hashlib.md5("\n".join(lines).encode()).hexdigest()


def gold_checksum(cur, table: str, sha: str, columns: list[str]) -> tuple[int, str]:
    cols = ", ".join(f"coalesce({c}::text, '')" for c in columns)
    cur.execute(f"select concat_ws('|', {cols}) from {table} where source_file_sha256 = %s", (sha,))
    lines = sorted(r[0] for r in cur.fetchall())
    return len(lines), hashlib.md5("\n".join(lines).encode()).hexdigest()


def count(cur, sql: str, *params) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def check_case(cur, feed: str, spec: dict, case: dict, sha: str, failures: list[str]) -> None:
    name = case["fixture"]
    channel = case.get("channel", "minio")
    cur.execute(
        "select array_agg(distinct channel) from ops.intake_delivery"
        " where feed = %s and file_sha256 = %s",
        (feed, sha),
    )
    got_channels = cur.fetchone()[0]
    if case["status"] != "quarantined" and got_channels != [channel]:
        failures.append(f"{name}: delivered by {got_channels}, expected [{channel!r}]")
    cur.execute(
        "select status from ops.intake_file where feed = %s and file_sha256 = %s", (feed, sha)
    )
    got = cur.fetchone()
    if not got or got[0] != case["status"]:
        failures.append(f"{name}: status {got and got[0]!r}, expected {case['status']!r}")
    cur.execute(
        "select distinct reason_code from ops.quarantine where feed = %s and file_sha256 = %s",
        (feed, sha),
    )
    reasons = sorted(r[0] for r in cur.fetchall())
    if reasons != sorted(case["reasons"]):
        failures.append(f"{name}: reasons {reasons}, expected {sorted(case['reasons'])}")

    got_gold = gold_checksum(cur, spec["gold_table"], sha, spec["checksum_columns"])
    if case["gold_rows"] == 0:
        if got_gold != EMPTY:
            failures.append(f"{name}: {got_gold[0]} rows reached gold, expected none")
        q = (
            "select count(*) from ops.quarantine"
            " where feed = %s and file_sha256 = %s and level = 'file'"
        )
        if count(cur, q, feed, sha) == 0:
            failures.append(f"{name}: no file-level quarantine row")
        for table, column in (
            ("bronze.csv_record", "file_sha256"),
            (spec["silver_table"], "file_sha256"),
        ):
            if count(cur, f"select count(*) from {table} where {column} = %s", sha):
                failures.append(f"{name}: rows from a quarantined file reached {table}")
        return
    want = fixture_checksum(
        (FIXTURES / name).read_bytes(), set(case.get("exclude_rows", [])), spec["checksum_columns"]
    )
    if got_gold != want or want[0] != case["gold_rows"]:
        failures.append(
            f"{name}: gold has {got_gold[0]} rows (checksum {got_gold[1][:12]}), expected"
            f" {case['gold_rows']} (fixture: {want[0]} rows, checksum {want[1][:12]})"
        )


def sftp_upload(feed: str, name: str, data: bytes) -> None:
    """Upload as the feed's vendor would: its own account and key, the pinned host key,
    into /upload/<feed>/ with a .done marker once the file is complete."""
    import base64
    import io as _io

    import paramiko

    account = feed.split(".")[0]
    key_type, key_b64 = os.environ["SFTP_HOST_KEY"].split()[:2]
    port = int(os.environ["SFTP_PORT"])
    ssh = paramiko.SSHClient()
    pinned = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64))
    ssh.get_host_keys().add(f"[127.0.0.1]:{port}", key_type, pinned)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    pkey = paramiko.Ed25519Key.from_private_key_file(
        str(Path(os.environ["SFTP_KEY_DIR"]) / f"{account}.key")
    )
    ssh.connect("127.0.0.1", port=port, username=account, pkey=pkey, look_for_keys=False,
                allow_agent=False, timeout=30)  # fmt: skip
    try:
        sftp = ssh.open_sftp()
        folder = f"/upload/{feed}"
        with contextlib.suppress(OSError):  # already there
            sftp.mkdir(folder)
        sftp.putfo(_io.BytesIO(data), f"{folder}/{name}")
        sftp.putfo(_io.BytesIO(b""), f"{folder}/{name}.done")
    finally:
        ssh.close()


def trigger_and_wait(token: str, dag_id: str, failures: list[str]) -> None:
    from smoke import AIRFLOW_URL, request, step, wait_for

    api = f"{AIRFLOW_URL}/api/v2"
    wait_for(
        f"DAG {dag_id} parsed",
        lambda: request("GET", f"{api}/dags/{dag_id}", token=token)[0] == 200,
    )
    request("PATCH", f"{api}/dags/{dag_id}", {"is_paused": False}, token=token)
    step(f"triggering {dag_id}")
    _, run = request("POST", f"{api}/dags/{dag_id}/dagRuns", {"logical_date": None}, token=token)
    run_id = run["dag_run_id"]

    def finished():
        _, b = request("GET", f"{api}/dags/{dag_id}/dagRuns/{run_id}", token=token)
        return b["state"] if b["state"] in ("success", "failed") else None

    state = wait_for(f"{dag_id} run to finish", finished)
    if state != "success":
        failures.append(f"{dag_id} run state is {state!r}")


def run_feed(s3, token: str, feed: str, spec: dict, failures: list[str]) -> None:
    import psycopg
    from smoke import step

    sha = {}
    by_sftp = [c for c in spec["cases"] if c.get("channel", "minio") == "sftp"]
    if by_sftp:
        step(f"{feed}: uploading {len(by_sftp)} fixture(s) over SFTP")
        for case in by_sftp:
            data = (FIXTURES / case["fixture"]).read_bytes()
            sha[case["fixture"]] = hashlib.sha256(data).hexdigest()
            sftp_upload(feed, Path(case["fixture"]).name, data)
        # The sync moves them to landing and triggers the feed's DAG itself; that run and
        # the one below never overlap (max_active_runs=1), and either one takes them.
        trigger_and_wait(token, "sftp_sync", failures)
    by_minio = [c for c in spec["cases"] if c.get("channel", "minio") == "minio"]
    step(f"{feed}: uploading {len(by_minio)} fixture(s) to MinIO")
    for case in by_minio:
        data = (FIXTURES / case["fixture"]).read_bytes()
        sha[case["fixture"]] = hashlib.sha256(data).hexdigest()
        s3.put_object(Bucket="landing", Key=f"{feed}/{Path(case['fixture']).name}", Body=data)
    trigger_and_wait(token, dag_id_for(feed), failures)

    step(f"{feed}: checking the warehouse")
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        for case in spec["cases"]:
            check_case(cur, feed, spec, case, sha[case["fixture"]], failures)


def main() -> int:
    import boto3
    from smoke import AIRFLOW_URL, MINIO_URL, PASSWORD, USER, request

    cases = load_cases()
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=os.environ["MINIO_USER"],
        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
        region_name="us-east-1",
    )
    _, body = request("POST", f"{AIRFLOW_URL}/auth/token", {"username": USER, "password": PASSWORD})
    failures: list[str] = []
    for feed, spec in cases["feeds"].items():
        run_feed(s3, body["access_token"], feed, spec, failures)
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    n = sum(len(s["cases"]) for s in cases["feeds"].values())
    print(f"PASS: {n} end-to-end case(s) across {len(cases['feeds'])} feed(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
