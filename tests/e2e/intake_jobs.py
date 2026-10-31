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

Env: MCP_URL, MCP_TRIAGE_KEY, MCP_TEAM_KEYS (Terraform's mcp_team_keys, JSON),
WAREHOUSE_DSN, and the MinIO and Airflow settings run.py uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import mcp_read
from break_loop import REPO, dip
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
    team_checks(check)
    model_call_checks(check)
    return report(failures)


TEAM_TOOLS = {"profile": "report_profile", "contract_author": "submit_candidate",
              "validator": "check_dbt_models", "reviewer": "report_review",
              "intake_orchestrator": "propose_contract_pr"}  # fmt: skip


def team_checks(check) -> None:
    """Stage 10 (S10.6): each intake-team key reaches the in-cluster MCP server and is
    listed its own tool (and not the orchestrator's, unless it is the orchestrator); and
    the validator's check_dbt_models runs `dbt parse` inside the MCP pod, on the models
    generated from a new feed's contract (dbt and the project are in the image)."""
    import psycopg

    keys = json.loads(os.environ.get("MCP_TEAM_KEYS") or "{}")
    check(sorted(keys) == sorted(TEAM_TOOLS), f"the team's keys: {sorted(keys)}")
    for agent, key in keys.items():
        s = mcp_read.Session(key).open()
        names = {t["name"] for t in s.post("tools/list")["tools"]}
        own = TEAM_TOOLS[agent] in names
        proposes = "propose_contract_pr" in names
        check(
            own and proposes == (agent == "intake_orchestrator"),
            f"{agent}: listed {TEAM_TOOLS[agent]}, propose_contract_pr only if orchestrator",
        )
    text = (REPO / "tests/fixtures/dbt_gen/purchase_orders.yaml").read_text("utf-8")
    cid = hashlib.sha256(text.encode()).hexdigest()[:16]
    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(
            "insert into ops.contract_candidate (chain_id, candidate_id, feed, file_sha256,"
            " contract_yaml, created_by) values ('e2e-dbt', %s, 'finance.purchase_orders',"
            " %s, %s, 'e2e') on conflict do nothing",
            (cid, "0" * 64, text),
        )
    s = mcp_read.Session(keys.get("validator")).open()
    # A cold `dbt parse` of the whole project can take a while in the pod (the server
    # allows 120 s).
    result = s.post("tools/call", {"name": "check_dbt_models",
                                   "arguments": {"candidate_id": cid,
                                                 "_run_id": "e2e-dbt/validate"}},
                    timeout=150)  # fmt: skip
    text = result["content"][0]["text"]
    try:
        out = json.loads(text)
    except ValueError:  # a tool error is plain text
        out = {"error": text[:300]}
    check(
        out.get("ok") is True
        and out.get("dbt_parse") is True
        and len(out.get("generated", [])) == 3,
        f"check_dbt_models parses the generated models in the MCP pod: {str(out)[:300]}",
    )


def model_call_checks(check) -> None:
    """Stage 11 (S11.1): ops.model_call exists in the deployed warehouse, no live call was
    recorded (CI has no model key), and the MCP server's login cannot read it."""
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select count(*) filter (where source = 'live') from ops.model_call")
        check(cur.fetchone() == (0,), "ops.model_call: no live model call without a key")
        cur.execute("select has_table_privilege('dip_mcp', 'ops.model_call', 'select')")
        check(cur.fetchone() == (False,), "ops.model_call: the MCP server's login cannot read it")


def report(failures: list[str]) -> int:
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: intake jobs queued from an onboarding sample and an executed handoff")
    return 0


if __name__ == "__main__":
    sys.exit(main())
