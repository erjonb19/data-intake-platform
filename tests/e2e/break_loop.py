"""Stage 4 (S4.5) end-to-end: one break-and-fix loop through the deployed stack, after the
file-feed cases (tests/e2e/run.py) and the MCP Read check.

The model-driven loop over every `dip break` scenario runs on recordings in CI's test job
(tests/integration/test_break_loop.py). Here a scripted MCP client stands in for the model,
because the cluster's quarantine (every E2E case's) changes from run to run and would void
a recording. What it proves is the deployed wiring:

  1. detect: the E2E's `elig_missing_member_id.csv` is quarantined
     (SCHEMA.MISSING_COLUMN:member_id), seen through the in-cluster MCP server with the
     triage agent's own key;
  2. propose: `propose_operation` (request_resend) writes a pending approval;
  3. the triage agent's call to `execute_approved_operation` is refused by its policy;
  4. approve and execute: a person, with `dip approvals approve` and `execute`;
  5. fix: the vendor's corrected file goes through MinIO and the intake DAG;
  6. check: it loaded, and the group's entries are closed as `superseded`.

The upgrade job runs the E2E twice on one cluster: on the second run the group is already
closed, and the check is that it was closed by a resend.

Env: MCP_URL, MCP_READER_KEY (mcp_read.Session), MCP_TRIAGE_KEY, WAREHOUSE_DSN, the MinIO
and Airflow settings run.py uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import mcp_read
from run import FIXTURES, dag_id_for, trigger_and_wait

FEED = "healthcare.eligibility"
GROUP = f"{FEED}|SCHEMA.MISSING_COLUMN:member_id"
REPO = Path(__file__).resolve().parents[2]


def dip(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DIP_WAREHOUSE_DSN": os.environ["WAREHOUSE_DSN"], "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-m", "dip", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> int:
    import boto3
    import psycopg
    from smoke import AIRFLOW_URL, MINIO_URL, PASSWORD, USER, request

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    s = mcp_read.Session(os.environ["MCP_TRIAGE_KEY"]).open()
    err, groups = s.call("list_quarantine_groups", feed=FEED)
    group = next((g for g in (groups or {}).get("groups", []) if g["group_id"] == GROUP), None)
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        missing = (FIXTURES / "eligibility/bad/elig_missing_member_id.csv").read_bytes()
        cur.execute(
            "select status, count(*) from ops.quarantine where feed = %s and"
            " file_sha256 = %s group by 1",
            (FEED, hashlib.sha256(missing).hexdigest()),
        )
        before = dict(cur.fetchall())
    if group is None:
        check(
            not err and before.get("superseded", 0) > 0 and "open" not in before,
            "the group was closed by a resend on an earlier run (upgrade job)",
        )
        return report(failures)
    check(not err, f"detect: the triage agent sees {GROUP} ({group['entries']} entries)")

    err, proposal = s.call(
        "propose_operation",
        action="request_resend",
        group_id=GROUP,
        quarantine_ids=group["quarantine_ids"],
        root_cause="schema_drift",
        confidence=0.9,
        rationale="member_id is missing from the file (E2E break loop)",
        message_to_vendor="Please resend the eligibility file with its member_id column.",
    )
    check(not err and proposal.get("status") == "pending", "propose: a pending approval")
    aid = proposal.get("approval_id") if isinstance(proposal, dict) else None
    err, denied = s.call("execute_approved_operation", approval_id=aid or 0)
    check(err and "denied by policy" in str(denied), "the triage agent cannot execute it (policy)")

    r = dip("approvals", "approve", str(aid), "--note", "E2E break loop")
    check(r.returncode == 0 and "approved" in r.stdout, f"approve: {r.stdout.strip()}")
    r = dip("approvals", "execute", str(aid))
    check(r.returncode == 0 and "vendor_request" in r.stdout, "execute: the resend request")

    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=os.environ["MINIO_USER"],
        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
        region_name="us-east-1",
    )
    resend = (FIXTURES / "eligibility/good/elig_resend.csv").read_bytes()
    s3.put_object(Bucket="landing", Key=f"{FEED}/elig_resend.csv", Body=resend)
    _, body = request("POST", f"{AIRFLOW_URL}/auth/token", {"username": USER, "password": PASSWORD})
    trigger_and_wait(body["access_token"], dag_id_for(FEED), failures)

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(
            "select status from ops.intake_file where feed = %s and file_sha256 = %s",
            (FEED, hashlib.sha256(resend).hexdigest()),
        )
        got = cur.fetchone()
        check(got == ("loaded",), f"fix: the corrected file loaded ({got})")
        cur.execute(
            "select status, count(*) from ops.quarantine where quarantine_id = any(%s) group by 1",
            (group["quarantine_ids"],),
        )
        after = dict(cur.fetchall())
        check(
            after == {"superseded": len(group["quarantine_ids"])},
            f"check: the group's entries are superseded ({json.dumps(after)})",
        )
        cur.execute(
            "select decision, count(*) from ops.audit_log where principal = 'triage'"
            " and tool = 'execute_approved_operation' group by 1"
        )
        check(dict(cur.fetchall()).get("denied", 0) >= 1, "the refused call is in the audit log")
    return report(failures)


def report(failures: list[str]) -> int:
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: break loop through the deployed stack (detect, propose, approve, fix, check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
