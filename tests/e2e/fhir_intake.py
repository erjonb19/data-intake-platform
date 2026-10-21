"""R1 end-to-end test: FHIR bundles dropped in MinIO go through the fhir_intake DAG.

Asserts that the good fixture lands in bronze, that each bad fixture is quarantined with
its expected reason code, and that none of the bad files' rows reach bronze. Safe to rerun:
already-processed files are recognized by sha256.

Env: as smoke.py, plus MINIO_USER, MINIO_PASSWORD and WAREHOUSE_DSN.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import boto3
import psycopg
from smoke import AIRFLOW_URL, MINIO_URL, PASSWORD, USER, request, step, wait_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fhir"
FEED = "healthcare.fhir_patient_bundle"
DAG_ID = "fhir_intake"
PATIENT = "8f3e6b1a-0000-4000-8000-000000000001"
EXPECTED_QUARANTINE = {
    "bad/patient_missing_birthdate.json": (
        "FHIR.PATIENT_INVALID:FHIR.MISSING_FIELD:Patient.birthDate"
    ),
    "bad/not_a_bundle.json": "FHIR.NOT_A_BUNDLE",
}


def main() -> int:
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=os.environ["MINIO_USER"],
        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
        region_name="us-east-1",
    )
    files = ["good/patient_t2d.json", *EXPECTED_QUARANTINE]
    sha = {}
    step(f"uploading {len(files)} fixture(s) to landing/{FEED}/")
    for name in files:
        data = (FIXTURES / name).read_bytes()
        sha[name] = hashlib.sha256(data).hexdigest()
        s3.put_object(Bucket="landing", Key=f"{FEED}/{Path(name).name}", Body=data)

    _, body = request("POST", f"{AIRFLOW_URL}/auth/token", {"username": USER, "password": PASSWORD})
    token = body["access_token"]
    api = f"{AIRFLOW_URL}/api/v2"
    wait_for("DAG parsed", lambda: request("GET", f"{api}/dags/{DAG_ID}", token=token)[0] == 200)
    request("PATCH", f"{api}/dags/{DAG_ID}", {"is_paused": False}, token=token)
    step(f"triggering {DAG_ID}")
    _, run = request("POST", f"{api}/dags/{DAG_ID}/dagRuns", {"logical_date": None}, token=token)
    run_id = run["dag_run_id"]

    def finished():
        _, b = request("GET", f"{api}/dags/{DAG_ID}/dagRuns/{run_id}", token=token)
        return b["state"] if b["state"] in ("success", "failed") else None

    failures: list[str] = []
    state = wait_for("DAG run to finish", finished)
    if state != "success":
        failures.append(f"{DAG_ID} run state is {state!r}")

    step("checking the warehouse")
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from bronze.fhir_resource where patient_id = %s", (PATIENT,))
        n = cur.fetchone()[0]
        if n != 9:
            failures.append(f"expected 9 bronze resources for the good patient, found {n}")
        for name, code in EXPECTED_QUARANTINE.items():
            cur.execute(
                "select reason_code from ops.quarantine where file_sha256 = %s and level = 'file'",
                (sha[name],),
            )
            codes = {r[0] for r in cur.fetchall()}
            if codes != {code}:
                failures.append(f"{name}: quarantine reasons {codes}, expected {{{code}}}")
            cur.execute(
                "select count(*) from bronze.fhir_resource where file_sha256 = %s", (sha[name],)
            )
            if cur.fetchone()[0]:
                failures.append(f"{name}: rows from a quarantined file reached bronze")
    for name in EXPECTED_QUARANTINE:
        keys = [
            o["Key"]
            for o in s3.list_objects_v2(Bucket="quarantine", Prefix=FEED).get("Contents", [])
        ]
        if not any(sha[name] in k for k in keys):
            failures.append(f"{name}: no object in the quarantine bucket")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: R1 FHIR intake end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
