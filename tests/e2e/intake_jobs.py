"""Stage 10 (S10.2) end-to-end: the two ways an intake job is queued, through the deployed
stack. Runs after the break loop (tests/e2e/break_loop.py).

  1. onboarding: a new vendor's sample is put in MinIO under
     landing/_onboard/finance.purchase_orders/; the agent_chain DAG's `intake_jobs` task
     registers it as a sample (ops.sample_file, its raw copy under raw/samples/) and queues
     an `onboard` job. The DAG run is green with no model key: nothing claims the job;
  2. drift: the triage agent's own key proposes `propose_contract_change` for an open
     quarantine group, a person approves and executes it with `dip approvals`, and a
     `drift` job for that feed is queued (or the feed already has one open).

The upgrade job runs the E2E twice on one cluster, so both checks accept a job queued by
the first run.

Env: MCP_URL, MCP_TRIAGE_KEY, WAREHOUSE_DSN, and the MinIO and Airflow settings run.py uses.
"""

from __future__ import annotations

import hashlib
import os
import sys

import mcp_read
from break_loop import dip
from run import trigger_and_wait

FEED = "finance.purchase_orders"
SAMPLE = b"po_number,vendor_id,order_date,amount,currency\nPO-0001,V-001,2026-10-29,120.50,USD\n"


def main() -> int:
    import boto3
    import psycopg
    from smoke import AIRFLOW_URL, MINIO_URL, PASSWORD, USER, request

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=os.environ["MINIO_USER"],
        aws_secret_access_key=os.environ["MINIO_PASSWORD"],
        region_name="us-east-1",
    )
    s3.put_object(Bucket="landing", Key=f"_onboard/{FEED}/po_20261030.csv", Body=SAMPLE)
    _, body = request("POST", f"{AIRFLOW_URL}/auth/token", {"username": USER, "password": PASSWORD})
    trigger_and_wait(body["access_token"], "agent_chain", failures)
    sha = hashlib.sha256(SAMPLE).hexdigest()
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from ops.sample_file where feed = %s and file_sha256 = %s",
                    (FEED, sha))  # fmt: skip
        check(cur.fetchone() == (1,), "onboarding: the sample is registered")
        cur.execute("select mode, status from ops.intake_job where feed = %s order by job_id",
                    (FEED,))  # fmt: skip
        jobs = cur.fetchall()
        check(bool(jobs) and jobs[0][0] == "onboard", f"onboarding: a job is queued ({jobs})")
        leftover = s3.list_objects_v2(Bucket="landing", Prefix=f"_onboard/{FEED}/")
        check(leftover.get("KeyCount", 0) == 0, "onboarding: the sample left landing")

        # A group nobody has a live proposal for (S9.5 allows one per group).
        cur.execute("select payload ->> 'group_id' from ops.approvals"
                    " where status in ('pending', 'approved')")  # fmt: skip
        busy = {r[0] for r in cur.fetchall()}
    s = mcp_read.Session(os.environ["MCP_TRIAGE_KEY"]).open()
    err, groups = s.call("list_quarantine_groups")
    group = next((g for g in (groups or {}).get("groups", []) if g["group_id"] not in busy), None)
    check(not err and group is not None, "drift: an open quarantine group to hand over")
    if group is None:
        return report(failures)
    err, proposal = s.call(
        "propose_operation",
        action="propose_contract_change",
        group_id=group["group_id"],
        quarantine_ids=group["quarantine_ids"],
        root_cause="schema_drift",
        confidence=0.6,
        rationale="E2E: hand the group to the intake team",
    )
    aid = proposal.get("approval_id") if isinstance(proposal, dict) else None
    check(not err and aid is not None, f"drift: a pending propose_contract_change ({proposal})")
    r = dip("approvals", "approve", str(aid), "--note", "E2E intake jobs")
    check(r.returncode == 0, f"drift: approved ({r.stdout.strip()} {r.stderr.strip()[-300:]})")
    r = dip("approvals", "execute", str(aid))
    check(r.returncode == 0, f"drift: executed ({r.stdout.strip()} {r.stderr.strip()[-300:]})")
    feed = group["group_id"].partition("|")[0]
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from ops.intake_job where feed = %s and mode = 'drift'",
                    (feed,))  # fmt: skip
        check(cur.fetchone()[0] >= 1, f"drift: a drift job for {feed} is queued")
    return report(failures)


def report(failures: list[str]) -> int:
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: intake jobs queued from an onboarding sample and an executed handoff")
    return 0


if __name__ == "__main__":
    sys.exit(main())
