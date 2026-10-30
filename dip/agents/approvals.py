"""Human approval of agent proposals (PLAN.md section 7, ops.approvals).

An agent's Propose-tier call creates a `pending` row: the action, its payload, and the
SHA-256 of the canonical payload. A person approves or rejects it (`dip approvals`).
Executing (Act tier) runs only a row that is `approved`, not expired, and whose payload
still hashes to both the hash recorded when it was proposed and the hash recorded when it
was approved. A trigger also refuses changes to a proposal's action, payload or hashes, so
through normal SQL an approved row cannot be edited at all; the hash checks are the second
line against someone who can disable triggers (the table owner or a superuser). Executed
rows record who ran them and the result; every step is audited.

Actions:
  request_resend           ask the vendor for a corrected file; no data changes, the
                           quarantine stays open until the new file arrives
  discard                  close the quarantine entries as discarded (the data is not
                           wanted and will not be resent)
  replay                   reprocess files quarantined whole: put each file's immutable raw
                           copy back into landing under its original name, so the next
                           intake validates it against the current contract (after a
                           contract fix). Their quarantine entries close as released
  propose_contract_change  hand the group to the intake team: its latest quarantined file
                           becomes a `drift` job in ops.intake_job (Stage 10, S10.2); no
                           data changes here
  contract_pr              the contract-author agent's candidate contract (Stage 5): written
                           as a commit on a new local branch, with the `git push` and
                           `gh pr create` for the person to run (dip/agents/contract_pr.py)
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from dip import BUCKET_LANDING, BUCKET_RAW
from dip.incidents import close_quarantine_incidents
from dip.storage import ObjectStore

# The operations propose_operation offers (the triage agent's); contract_pr is created only
# by propose_contract_pr, with its own payload.
OPERATIONS = ("request_resend", "discard", "replay", "propose_contract_change")
ACTIONS = (*OPERATIONS, "contract_pr")
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
            "select approval_id, agent_id, action, payload, payload_sha256, status, expires_at,"
            " approved_sha256, decided_by from ops.approvals where approval_id = %s"
            + (" for update" if lock else ""),
            (approval_id,),
        )
        row = cur.fetchone()
    if row is None:
        conn.rollback()
        raise ApprovalError(f"no approval {approval_id}")
    keys = ("approval_id", "agent_id", "action", "payload", "payload_sha256", "status",
            "expires_at", "approved_sha256", "decided_by")  # fmt: skip
    return dict(zip(keys, row, strict=True))


def decide(conn: Any, approval_id: int, approve: bool, by: str, note: str = "") -> str:
    """Approve or reject a pending proposal. An approval records the hash of the payload the
    person approved, so the row that runs must still be that payload."""
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
            " decision_note = %s, approved_sha256 = %s where approval_id = %s",
            (
                status,
                by,
                note,
                payload_sha256(a["payload"]) if status == "approved" else None,
                approval_id,
            ),  # fmt: skip
        )
    conn.commit()
    return status


def check_agent_may_execute(conn: Any, approval_id: int, agent: str, actions: list[str]) -> None:
    """Refuse (Denied) unless an agent may execute this approval now (Stage 6): a person
    approved it (`decided_by` is `human:...`), it is unexpired, its payload still hashes to
    what was proposed and approved, this agent proposed it, and its action is one the
    agent's policy lets it execute. Read only: a refused call changes nothing, not even an
    expired row's status. `execute` checks status, expiry and hashes again under the lock."""
    from dip.agents.registry import Denied

    with conn.cursor() as cur:
        cur.execute(
            "select agent_id, action, payload, payload_sha256, approved_sha256, status,"
            " decided_by, expires_at from ops.approvals where approval_id = %s",
            (approval_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise Denied(f"no approval {approval_id}")
    proposer, action, payload, proposed, approved, status, decided_by, expires = row
    if status != "approved":
        raise Denied(f"approval {approval_id} is {status}: only a person's approval lets it run")
    if not (decided_by or "").startswith("human:"):
        raise Denied(f"approval {approval_id} was not approved by a person ({decided_by})")
    if proposer != agent:
        raise Denied(f"approval {approval_id} was proposed by {proposer}, not {agent}")
    if action not in actions:
        raise Denied(f"{agent} may not execute a {action!r} approval (allowed: {actions})")
    if expires <= datetime.now(UTC):
        raise Denied(f"approval {approval_id} expired at {expires.isoformat()}")
    if not (payload_sha256(payload) == proposed == approved):
        raise Denied(f"approval {approval_id}: payload changed since it was proposed")
    # An action this server's database login cannot complete is refused here, before
    # execute would mark a person's approval `failed` (in the cluster, dip_mcp cannot write
    # quarantine or intake rows: discard and replay there are a person's to run).
    needs = {"discard": [("ops.quarantine", "UPDATE")],
             "replay": [("ops.quarantine", "UPDATE"), ("ops.intake_file", "DELETE"),
                        ("ops.intake_delivery", "DELETE")]}.get(action, [])  # fmt: skip
    with conn.cursor() as cur:
        missing = []
        for table, privilege in needs:
            cur.execute("select has_table_privilege(%s, %s)", (table, privilege))
            if not cur.fetchone()[0]:
                missing.append(f"{privilege} on {table}")
    conn.commit()
    if missing:
        raise Denied(f"this server's database login cannot run a {action} ({', '.join(missing)});"
                     " a person runs it with `dip approvals execute`")  # fmt: skip


def execute(conn: Any, store: ObjectStore, approval_id: int, by: str,
            agent_actions: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Run an approved proposal, once. The row stays locked from the check to the status
    update, and the action's database changes and `executed` commit together, so two
    concurrent executions cannot both run it and a crash in between leaves it approved
    with nothing done. For an agent (`agent_actions` given), the agent's conditions are
    checked again here, under the lock: a person approved it, this agent proposed it, and
    its action is one the agent may run."""
    a = _get(conn, approval_id, lock=True)
    if a["status"] != "approved":
        conn.rollback()
        raise ApprovalError(f"approval {approval_id} is {a['status']}, not approved")
    if agent_actions is not None and not (
        (a["decided_by"] or "").startswith("human:")
        and a["agent_id"] == by
        and a["action"] in agent_actions
    ):
        conn.rollback()
        raise ApprovalError(f"approval {approval_id} is not one {by} may run")
    if a["expires_at"] <= datetime.now(UTC):
        with conn.cursor() as cur:
            cur.execute("update ops.approvals set status = 'expired' where approval_id = %s",
                        (approval_id,))  # fmt: skip
        conn.commit()
        raise ApprovalError(f"approval {approval_id} expired before it ran")
    digest = payload_sha256(a["payload"])
    if not (digest == a["payload_sha256"] == a["approved_sha256"]):
        conn.rollback()
        raise ApprovalError(f"approval {approval_id}: payload changed since it was proposed")
    landed: list[str] = []
    try:
        result = _run(conn, store, a["action"], a["payload"], landed, approval_id, by)
        with conn.cursor() as cur:
            cur.execute(
                "update ops.approvals set status = 'executed', executed_by = %s,"
                " executed_at = now(), result = %s::jsonb where approval_id = %s",
                (by, json.dumps(result, sort_keys=True), approval_id),
            )
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        for key in landed:  # replay: undo what reached landing, the database did not change
            store.delete(BUCKET_LANDING, key)
        error = f"{type(exc).__name__}: {exc}"
        a = _get(conn, approval_id, lock=True)
        if a["status"] == "approved":
            with conn.cursor() as cur:
                cur.execute(
                    "update ops.approvals set status = 'failed', executed_by = %s,"
                    " executed_at = now(), result = %s::jsonb where approval_id = %s",
                    (by, json.dumps({"error": error}), approval_id),
                )
        conn.commit()
        raise ApprovalError(f"approval {approval_id} failed: {error}") from exc


def _open_ids(cur: Any, ids: list[int]) -> list[int]:
    cur.execute(
        "select quarantine_id from ops.quarantine where quarantine_id = any(%s)"
        " and status = 'open' order by 1",
        (ids,),
    )
    return [r[0] for r in cur.fetchall()]


def _run(conn: Any, store: ObjectStore, action: str, payload: dict, landed: list[str],
         approval_id: int = 0, by: str = "") -> dict:  # fmt: skip
    """The action's database changes, uncommitted (execute commits them with the status).
    Replay's files go into landing here; `landed` lists them so a failure can undo them."""
    if action == "contract_pr":  # no database change: a local branch for a person to publish
        from dip.agents.contract_pr import write_branch

        return write_branch(payload, approval_id, by)
    ids = [int(i) for i in payload["quarantine_ids"]]
    with conn.cursor() as cur:
        open_ids = _open_ids(cur, ids)
        if action == "discard":
            cur.execute(
                "update ops.quarantine set status = 'discarded' where quarantine_id = any(%s)",
                (open_ids,),
            )
            close_quarantine_incidents(cur, payload["group_id"].partition("|")[0])
            return {"discarded": open_ids}
        if action == "replay":
            cur.execute(
                "select distinct q.feed, q.file_sha256, f.raw_key, f.source_key"
                " from ops.quarantine q join ops.intake_file f"
                "   on f.feed = q.feed and f.file_sha256 = q.file_sha256"
                " where q.quarantine_id = any(%s) and f.status = 'quarantined'"
                " order by 1, 2",
                (open_ids,),
            )
            files = [(feed, sha, raw, f"{feed}/{PurePosixPath(src).name}")
                     for feed, sha, raw, src in cur.fetchall()]  # fmt: skip
            if not files:
                raise ApprovalError("nothing to replay: no open quarantine of a whole file")
            # Every key is checked before anything changes.
            busy = [key for *_, key in files if store.exists(BUCKET_LANDING, key)]
            if busy:
                raise ApprovalError(f"waiting in landing, replay later: {busy}")
            for feed, sha, _, _ in files:
                # Forget that the content was processed, so the intake validates it again.
                # Its quarantine history stays, closed as released; note that this closes
                # every open entry of the file, also those of other groups, and that the
                # file leaves the feed history until it is processed again.
                cur.execute("delete from ops.intake_delivery where feed = %s and file_sha256 = %s",
                            (feed, sha))  # fmt: skip
                cur.execute("delete from ops.intake_file where feed = %s and file_sha256 = %s",
                            (feed, sha))  # fmt: skip
                cur.execute(
                    "update ops.quarantine set status = 'released' where feed = %s"
                    " and file_sha256 = %s and status = 'open'",
                    (feed, sha),
                )
                close_quarantine_incidents(cur, feed)
            for _, _, raw_key, key in files:
                store.put(BUCKET_LANDING, key, store.get(BUCKET_RAW, raw_key))
                landed.append(key)
            return {"relanded": landed[:]}
        if action == "request_resend":
            return {"vendor_request": payload.get("message") or payload.get("rationale", ""),
                    "quarantine_ids": open_ids}  # fmt: skip
        if action == "propose_contract_change":
            # Stage 10 (S10.2): the group's latest quarantined file becomes a drift job
            # for the intake team (dip/intake_jobs.py), which ends in a contract proposal
            # for a person, never a change on its own.
            from dip.intake_jobs import drift_from_handoff

            feed = payload["group_id"].partition("|")[0]
            return {**drift_from_handoff(cur, feed, open_ids, approval_id),
                    "quarantine_ids": open_ids}  # fmt: skip
        raise ApprovalError(f"unknown action {action!r}")


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
