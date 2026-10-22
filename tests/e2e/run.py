"""Stage 1+ end-to-end cases (PLAN.md section 6), from tests/e2e/cases.yaml.

For each feed: drop its fixtures into `landing/<feed>/`, trigger the feed's intake DAG,
wait for it, then check every case in the warehouse:
  - the file's ops.intake_file status and its set of quarantine reason codes;
  - a file quarantined whole has a file-level quarantine row and nothing of it is in
    bronze, silver or gold;
  - otherwise its rows in gold: the expected count, and a checksum over every contract
    column computed independently from the fixture (minus `exclude_rows`).

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


def run_feed(s3, token: str, feed: str, spec: dict, failures: list[str]) -> None:
    import psycopg
    from smoke import AIRFLOW_URL, request, step, wait_for

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
