"""Stage 9 (S9.2) end-to-end: the deployed approvals page, through a port-forward, after
the break loop (tests/e2e/break_loop.py) has left an executed approval and a denied Act
call behind. The page runs as its own login (dip_approver): it lists and shows the
proposal, finds the denial in the audit log, refuses a decision on a row that is no longer
pending, and refuses a Host header it was not told to accept.

Env: APPROVALS_URL (http://127.0.0.1:<port>), APPROVALS_TOKEN (its bearer token).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ["APPROVALS_URL"]
TOKEN = os.environ["APPROVALS_TOKEN"]


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(
        URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            is_json = r.headers.get_content_type() == "application/json"
            return r.status, (json.loads(raw) if is_json else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    status, page = call("GET", "/")
    check(status == 200 and "<title>Approvals</title>" in page, "the page is served")
    status, rows = call("GET", "/api/approvals?status=all")
    check(status == 200 and isinstance(rows, list), f"list every proposal ({status})")
    # The break loop's request_resend, which triage ran (S10.2's intake_jobs E2E leaves an
    # executed propose_contract_change behind too, executed by a person).
    done = [r for r in rows if r["agent_id"] == "triage" and r["status"] == "executed"
            and r["action"] == "request_resend"]  # fmt: skip
    check(bool(done), "the break loop's executed request_resend is listed")
    if done:
        aid = done[0]["approval_id"]
        status, shown = call("GET", f"/api/approvals/{aid}")
        check(
            status == 200
            and shown["decided_by"].startswith("human:")
            and shown["executed_by"] == "triage",
            f"show #{aid}: a person decided, triage ran it",
        )
        status, body = call(
            "POST", f"/api/approvals/{aid}/decision", {"decision": "reject"}, {"X-Reviewer": "e2e"}
        )
        check(status == 409 and "not pending" in body, f"deciding an executed row: {status}")
    status, denied = call("GET", "/api/audit?decision=denied&tool=execute_approved_operation")
    check(
        status == 200 and any(r["principal"] == "triage" for r in denied),
        "the audit log shows the Act call denied before approval",
    )
    status, refused = call("GET", "/api/audit?decision=denied&tool=approvals.reject")
    check(
        status == 200 and any(r["principal"] == "human:e2e@dip_approver" for r in refused),
        "the page's refused decision is audited as dip_approver",
    )
    # Stage 11 (S11.2): a chain's trace. intake_jobs.py ran the validator's check_dbt_models
    # under the chain e2e-dbt (run id e2e-dbt/validate), so its tool span is there.
    status, t = call("GET", "/api/trace?chain=e2e-dbt")
    check(
        status == 200 and any(s["kind"] == "tool" and s["name"] == "check_dbt_models"
                              for s in t.get("spans", [])),
        f"a chain's trace: {status} {str(t)[:200]}",
    )  # fmt: skip
    status, _ = call("GET", "/api/approvals", headers={"Authorization": "Bearer wrong"})
    check(status == 401, f"a wrong token is refused ({status})")
    status, _ = call("GET", "/api/approvals", headers={"Host": "evil.example:8081"})
    check(status == 400, f"an unknown Host header is refused ({status})")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
