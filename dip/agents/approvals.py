"""Human approval of agent proposals (PLAN.md section 7, ops.approvals).

An agent's Propose-tier call creates a `pending` row: the action, its payload, and the
SHA-256 of the canonical payload. A person approves or rejects it (`dip approvals`).
Executing (Act tier) runs only a row that is `approved`, not expired, and whose payload
still hashes to the hash recorded when it was proposed, so a row edited after approval
never runs. Executed rows record who ran them and the result; every step is audited.

Actions:
  request_resend           ask the vendor for a corrected file; no data changes, the
                           quarantine stays open until the new file arrives
  discard                  close the quarantine entries as discarded (the data is not
                           wanted and will not be resent)
  replay                   reprocess files quarantined whole: put each file's immutable raw
                           copy back into landing under its original name, so the next
                           intake validates it against the current contract (after a
                           contract fix). Their quarantine entries close as released
  propose_contract_change  hand the group to the contract-author agent (Stage 5); no data
                           changes here
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from dip import BUCKET_LANDING, BUCKET_RAW
from dip.storage import ObjectStore

ACTIONS = ("request_resend", "discard", "replay", "propose_contract_change")
TTL = timedelta(days=7)


class ApprovalError(RuntimeError):
    pass


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def create(conn: Any, agent_id: str, run_id: str | None, action: str, payload: dict) -> int:
    if action not in ACTIONS:
        raise ApprovalError(f"unknown action {action!r}")
    with conn.cursor() as cur:
        cur.execute(
            "insert into ops.approvals (agent_id, run_id, action, payload, payload_sha256,"
            " expires_at) values (%s, %s, %s, %s::jsonb, %s, now() + %s) returning approval_id",
            (
                agent_id,
                run_id,
                action,
                json.dumps(payload, sort_keys=True),
                payload_sha256(payload),
                TTL,
            ),  # fmt: skip
        )
        (approval_id,) = cur.fetchone()
    conn.commit()
    return approval_id


def _get(conn: Any, approval_id: int, lock: bool = False) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "select approval_id, agent_id, action, payload, payload_sha256, status, expires_at"
            " from ops.approvals where approval_id = %s" + (" for update" if lock else ""),
            (approval_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ApprovalError(f"no approval {approval_id}")
    keys = ("approval_id", "agent_id", "action", "payload", "payload_sha256", "status",
            "expires_at")  # fmt: skip
    return dict(zip(keys, row, strict=True))


def decide(conn: Any, approval_id: int, approve: bool, by: str, note: str = "") -> str:
    a = _get(conn, approval_id, lock=True)
    if a["status"] != "pending":
        conn.rollback()
        raise ApprovalError(f"approval {approval_id} is {a['status']}, not pending")
    status = "approved" if approve else "rejected"
    if a["expires_at"] <= datetime.now(UTC):
        status = "expired"
    with conn.cursor() as cur:
        cur.execute(
            "update ops.approvals set status = %s, decided_by = %s, decided_at = now(),"
            " decision_note = %s where approval_id = %s",
            (status, by, note, approval_id),
        )
    conn.commit()
    return status


def execute(conn: Any, store: ObjectStore, approval_id: int, by: str) -> dict[str, Any]:
    a = _get(conn, approval_id, lock=True)
    if a["status"] != "approved":
        conn.rollback()
        raise ApprovalError(f"approval {approval_id} is {a['status']}, not approved")
    if a["expires_at"] <= datetime.now(UTC):
        with conn.cursor() as cur:
            cur.execute("update ops.approvals set status = 'expired' where approval_id = %s",
                        (approval_id,))  # fmt: skip
        conn.commit()
        raise ApprovalError(f"approval {approval_id} expired before it ran")
    if payload_sha256(a["payload"]) != a["payload_sha256"]:
        conn.rollback()
        raise ApprovalError(f"approval {approval_id}: payload changed since it was proposed")
    try:
        result = _run(conn, store, a["action"], a["payload"], approval_id)
        status = "executed"
    except Exception as exc:
        conn.rollback()
        result, status = {"error": f"{type(exc).__name__}: {exc}"}, "failed"
    with conn.cursor() as cur:
        cur.execute(
            "update ops.approvals set status = %s, executed_by = %s, executed_at = now(),"
            " result = %s::jsonb where approval_id = %s",
            (status, by, json.dumps(result, sort_keys=True), approval_id),
        )
    conn.commit()
    if status == "failed":
        raise ApprovalError(f"approval {approval_id} failed: {result['error']}")
    return result


def _open_ids(cur: Any, ids: list[int]) -> list[int]:
    cur.execute(
        "select quarantine_id from ops.quarantine where quarantine_id = any(%s)"
        " and status = 'open' order by 1",
        (ids,),
    )
    return [r[0] for r in cur.fetchall()]


def _run(conn: Any, store: ObjectStore, action: str, payload: dict, approval_id: int) -> dict:
    ids = [int(i) for i in payload["quarantine_ids"]]
    with conn.cursor() as cur:
        open_ids = _open_ids(cur, ids)
        if action == "discard":
            cur.execute(
                "update ops.quarantine set status = 'discarded' where quarantine_id = any(%s)",
                (open_ids,),
            )
            result = {"discarded": open_ids}
        elif action == "replay":
            cur.execute(
                "select distinct q.feed, q.file_sha256, f.raw_key, f.source_key"
                " from ops.quarantine q join ops.intake_file f"
                "   on f.feed = q.feed and f.file_sha256 = q.file_sha256"
                " where q.quarantine_id = any(%s) and f.status = 'quarantined'",
                (open_ids,),
            )
            files = cur.fetchall()
            relanded = []
            for feed, sha, raw_key, source_key in files:
                data = store.get(BUCKET_RAW, raw_key)
                key = f"{feed}/{PurePosixPath(source_key).name}"
                if store.exists(BUCKET_LANDING, key):
                    raise ApprovalError(f"{key} is waiting in landing; replay later")
                # Forget that the content was processed, so the intake validates it again;
                # its quarantine history stays, closed as released by this approval.
                cur.execute("delete from ops.intake_delivery where feed = %s and file_sha256 = %s",
                            (feed, sha))  # fmt: skip
                cur.execute("delete from ops.intake_file where feed = %s and file_sha256 = %s",
                            (feed, sha))  # fmt: skip
                cur.execute(
                    "update ops.quarantine set status = 'released' where feed = %s"
                    " and file_sha256 = %s and status = 'open'",
                    (feed, sha),
                )
                store.put(BUCKET_LANDING, key, data)
                relanded.append(key)
            if not relanded:
                raise ApprovalError("nothing to replay: no open quarantine of a whole file")
            result = {"relanded": relanded}
        elif action == "request_resend":
            result = {"vendor_request": payload.get("message") or payload.get("rationale", ""),
                      "quarantine_ids": open_ids}  # fmt: skip
        elif action == "propose_contract_change":
            result = {"handoff": "contract-author agent (Stage 5)", "quarantine_ids": open_ids}
        else:
            raise ApprovalError(f"unknown action {action!r}")
    conn.commit()
    return result


def pending(conn: Any, status: str | None = "pending") -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select approval_id, created_at, agent_id, action, payload, status, expires_at"
            " from ops.approvals where %s::text is null or status = %s order by approval_id",
            (status, status),
        )
        keys = ("approval_id", "created_at", "agent_id", "action", "payload", "status",
                "expires_at")  # fmt: skip
        rows = [dict(zip(keys, r, strict=True)) for r in cur.fetchall()]
    conn.commit()
    return rows
