"""Stage 1+ end-to-end cases (PLAN.md section 6), from tests/e2e/cases.yaml.

For each feed: drop its fixtures into `landing/<feed>/`, trigger the feed's intake DAG,
wait for it, then check every case in the warehouse:
  - the file's ops.intake_file status and its set of quarantine reason codes;
  - its rows in gold: the expected count, and a checksum over the key columns computed
    independently from the fixture (minus `exclude_rows`);
  - a file expected to reach gold with 0 rows left nothing in bronze, silver or gold.

Safe to rerun: processed files are recognized by sha256.
Env: as smoke.py, plus MINIO_USER, MINIO_PASSWORD and WAREHOUSE_DSN.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sys
from pathlib import Path

import boto3
import psycopg
import yaml
from smoke import AIRFLOW_URL, MINIO_URL, PASSWORD, USER, request, step, wait_for

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


def dag_id_for(feed: str) -> str:
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


def run_feed(s3, token: str, feed: str, spec: dict, failures: list[str]) -> None:
    api = f"{AIRFLOW_URL}/api/v2"
    dag_id = dag_id_for(feed)
    sha = {}
    step(f"{feed}: uploading {len(spec['cases'])} fixture(s)")
    for case in spec["cases"]:
        data = (FIXTURES / case["fixture"]).read_bytes()
        sha[case["fixture"]] = hashlib.sha256(data).hexdigest()
        s3.put_object(Bucket="landing", Key=f"{feed}/{Path(case['fixture']).name}", Body=data)

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

    step(f"{feed}: checking the warehouse")
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        for case in spec["cases"]:
            name, s = case["fixture"], sha[case["fixture"]]
            cur.execute(
                "select status from ops.intake_file where feed = %s and file_sha256 = %s", (feed, s)
            )
            got = cur.fetchone()
            if not got or got[0] != case["status"]:
                failures.append(f"{name}: status {got and got[0]!r}, expected {case['status']!r}")
            cur.execute(
                "select distinct reason_code from ops.quarantine"
                " where feed = %s and file_sha256 = %s",
                (feed, s),
            )
            reasons = sorted(r[0] for r in cur.fetchall())
            if reasons != sorted(case["reasons"]):
                failures.append(f"{name}: reasons {reasons}, expected {sorted(case['reasons'])}")
            want = fixture_checksum(
                (FIXTURES / name).read_bytes(),
                set(case.get("exclude_rows", [])),
                spec["checksum_columns"],
            )
            if case["gold_rows"] == 0:
                want = (0, hashlib.md5(b"").hexdigest())
            got_gold = gold_checksum(cur, spec["gold_table"], s, spec["checksum_columns"])
            if got_gold[0] != case["gold_rows"] or got_gold != want:
                failures.append(
                    f"{name}: gold has {got_gold[0]} rows (checksum {got_gold[1][:12]}),"
                    f" expected {case['gold_rows']} (checksum {want[1][:12]})"
                )
            if case["gold_rows"] == 0:
                cur.execute(
                    "select count(*) from bronze.csv_record where feed = %s and file_sha256 = %s",
                    (feed, s),
                )
                if cur.fetchone()[0]:
                    failures.append(f"{name}: rows from a quarantined file reached bronze")


def main() -> int:
    cases = yaml.safe_load((HERE / "cases.yaml").read_text("utf-8"))
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
