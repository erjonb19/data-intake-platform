"""Stage 3 (S3.4) end-to-end check of the deployed MCP Read tier, after the file-feed cases.

Speaks MCP's Streamable HTTP directly (JSON-RPC over POST; replies as JSON or one SSE
event), so the E2E needs no SDK:
  - an unauthenticated request and a wrong key are refused (401);
  - the `reader` agent is listed its policy's read tools and the Act tool, nothing else;
  - the Stage 3 demo question, "why did the eligibility feed quarantine rows?", is answered
    from MCP alone: the open groups, a sample with the rejected record, the feed history,
    the DAG run and its intake task's log, and (observability profile) the metric and the
    lineage behind it;
  - a propose call is refused by the policy, and every call is in ops.audit_log.

Env: MCP_URL, MCP_READER_KEY, WAREHOUSE_DSN, OBSERVABILITY (1 when that profile is on).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MCP = os.environ["MCP_URL"].rstrip("/") + "/mcp"
KEY = os.environ["MCP_READER_KEY"]
OBS = os.environ.get("OBSERVABILITY") == "1"
READ_TOOLS = {"list_quarantine_groups", "get_quarantine_sample", "get_contract",
              "get_feed_history", "get_dq_results", "get_dag_run", "get_task_log",
              "get_lineage", "query_metrics", "profile_file"}  # fmt: skip


class Session:
    def __init__(self, key: str | None):
        self.key, self.sid, self.n = key, None, 0

    def post(self, method: str, params: dict | None = None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}  # fmt: skip
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(MCP, json.dumps(msg).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            body = r.read().decode()
        if notify or not body.strip():
            return None
        if body.lstrip().startswith("{"):
            reply = json.loads(body)
        else:  # one SSE event: "event: message\ndata: {...}"
            data = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
            reply = json.loads(data[-1])
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def open(self):
        self.post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "dip-e2e", "version": "1"}})  # fmt: skip
        self.post("notifications/initialized", notify=True)
        return self

    def call(self, tool: str, **args):
        result = self.post("tools/call", {"name": tool, "arguments": args})
        text = result["content"][0]["text"]
        return result.get("isError", False), (json.loads(text) if text[:1] in "{[" else text)


MISSING_SHA = hashlib.sha256(
    (
        Path(__file__).resolve().parent.parent
        / "fixtures/eligibility/bad/elig_missing_member_id.csv"
    ).read_bytes()
).hexdigest()


def resent_earlier() -> bool:
    """The missing-member file's entries were closed by a loaded resend (superseded)."""
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select bool_and(status = 'superseded') from ops.quarantine"
                    " where file_sha256 = %s", (MISSING_SHA,))  # fmt: skip
        return bool(cur.fetchone()[0])


def refused(key: str | None) -> bool:
    try:
        Session(key).open()
    except urllib.error.HTTPError as e:
        return e.code == 401
    return False


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str):
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    check(refused(None), "an unauthenticated request is refused (401)")
    check(refused("not-the-key"), "a wrong key is refused (401)")
    s = Session(KEY).open()
    listed = {t["name"] for t in s.post("tools/list")["tools"]}
    check(
        listed == READ_TOOLS | {"execute_approved_operation"},
        f"reader is listed its read tools and the Act tool ({len(listed)} tools)",
    )

    feed = "healthcare.eligibility"
    _, groups = s.call("list_quarantine_groups", feed=feed)
    codes = {g["reason_code"]: g for g in groups["groups"]}
    # The missing-column group is open, unless an earlier run's break loop (the upgrade
    # job runs the E2E twice) closed it with a resend (tests/e2e/break_loop.py).
    missing_open = "SCHEMA.MISSING_COLUMN:member_id" in codes or resent_earlier()
    check(
        "VALUE.NOT_ALLOWED:plan_code" in codes and missing_open,
        f"open quarantine groups for {feed}: {sorted(codes)}",
    )
    group = codes.get("VALUE.NOT_ALLOWED:plan_code", {"group_id": f"{feed}|x"})
    _, sample = s.call("get_quarantine_sample", group_id=group["group_id"], limit=3)
    rec = (sample.get("untrusted_vendor_content") or [{}])[0].get("record") or {}
    check(rec.get("plan_code") == "GOLD", "the sample shows the rejected record (plan_code GOLD)")
    check(rec.get("first_name") == "(PHI: not shown)", "the reader never sees a PHI value")
    err, prof = s.call("profile_file", feed=feed, file_sha256=MISSING_SHA[:12])
    check(
        not err and "member_id" in prof.get("missing_contract_columns", []),
        "profile_file reads the raw copy (MinIO read-only user) and finds the missing column",
    )
    err, dq = s.call("get_dq_results", model="member_eligibility")
    check(not err and bool(dq.get("results")), "DQ results for gold.member_eligibility")
    _, history = s.call("get_feed_history", feed=feed)
    check(len(history["files"]) >= 3, f"feed history: {len(history['files'])} files")
    _, contract = s.call("get_contract", feed=feed)
    allowed = next(c for c in contract["columns"] if c["name"] == "plan_code")["allowed"]
    check("GOLD" not in allowed, f"the contract allows {allowed}: GOLD is the vendor's change")
    err, runs = s.call("get_dag_run", dag_id="intake__healthcare__eligibility", limit=10)
    recent = (runs.get("runs") or []) if not err else []
    run = recent[0] if recent else {}
    check(not err and run.get("state") == "success", f"the DAG run: {run.get('state')}")
    # The feed's DAG runs several times in the E2E (each channel triggers it), and a run
    # that finds landing empty logs no file; one of the recent runs processed the files.
    seen, last = False, None
    for r in recent:
        err, log = s.call("get_task_log", dag_id="intake__healthcare__eligibility",
                          dag_run_id=r["dag_run_id"], task_id="intake")  # fmt: skip
        last = log
        text = "\n".join(map(str, log.get("untrusted_vendor_content", []))) if not err else ""
        if "accepted=" in text:
            seen = True
            break
    if not seen:  # what came back, for the diagnosis
        print(f"    get_task_log: {str(last)[:600]}", file=sys.stderr)
    check(seen, f"a recent intake task's log shows the files it processed ({len(recent)} runs)")
    if OBS:
        _, m = s.call("query_metrics", metric="open_quarantine", feed=feed)
        plan = [
            x
            for x in m["series"]
            if x["labels"].get("reason_code") == "VALUE.NOT_ALLOWED:plan_code"
        ]
        check(bool(plan), "the metric shows the open plan_code quarantine")
        _, lin = s.call("get_lineage", table="gold.member_eligibility")
        check(any("s3://landing" in u for u in lin.get("untrusted_vendor_content", [])),
              "lineage from gold back to the landed files")  # fmt: skip
    err, denied = s.call("propose_operation", action="discard", group_id=group["group_id"],
                         quarantine_ids=[1], root_cause="vendor_change", rationale="x",
                         confidence=0.5)  # fmt: skip
    check(err and "denied by policy" in str(denied), "a propose call is refused by the policy")

    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select decision, count(*) from ops.audit_log where principal = 'reader'"
                    " group by 1")  # fmt: skip
        audited = dict(cur.fetchall())
    check(
        audited.get("allowed", 0) >= 6 and audited.get("denied", 0) >= 1,
        f"every call is audited: {audited}",
    )
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: MCP Read tier (auth, policy, the demo question, audit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
