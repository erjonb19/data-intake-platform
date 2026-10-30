"""The approvals page and audit viewer (Stage 9, S9.2; PLAN.md section 7's "simple web page").

A person sees what the agents proposed, approves or rejects it, and reads the audit log,
in a browser instead of `dip approvals`. Served by `python -m dip approvals-serve` (in the
cluster: the `approvals` Deployment, ClusterIP, a port-forward for local use).

What it may do is what its database login may do, `dip_approver`: read ops.approvals and
ops.audit_log, append to the audit log, and set an approval's decision columns. It cannot
execute an approval (that stays `dip approvals execute`, or an agent's Act tier after a
person approved), and the approvals table's trigger still holds it to the one-way status
machine. Decisions go through `dip.agents.approvals.decide`, the CLI's function, so the
approval rules (pending only, expiry, the approved payload's hash) live in one place.

Who decided: the `X-Reviewer` header, recorded as `human:<name>@<database login>`, the
same shape as the CLI's `human:<OS user>@<database login>`. Like the CLI's, the name is
self-asserted: anyone who can reach this page can decide, as anyone with the warehouse
DSN can with the CLI. Two browser-side guards: the Host header must be one this server
was told to accept (a DNS-rebinding page cannot reach it through a port-forward), and a
decision needs the custom header and a JSON body, which a cross-site form cannot send.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dip.agents import approvals
from dip.agents.registry import Registry, human_policy

REVIEWER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
STATUSES = ("pending", "approved", "rejected", "expired", "executed", "failed")
STATUS_PATTERN = f"^(all|{'|'.join(STATUSES)})$"
DETAIL = ("approval_id", "created_at", "agent_id", "run_id", "action", "payload",
          "payload_sha256", "status", "expires_at", "decided_by", "decided_at",
          "decision_note", "approved_sha256", "executed_by", "executed_at", "result")  # fmt: skip
AUDIT = ("audit_id", "at", "principal", "run_id", "tool", "tier", "decision", "reason",
         "result_status", "latency_ms")  # fmt: skip


def _db(request: Request) -> Iterator[Any]:
    conn = request.app.state.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[Any, Depends(_db)]


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=2000)


def _summary(a: dict[str, Any]) -> dict[str, Any]:
    p = a["payload"] or {}
    return {
        "approval_id": a["approval_id"],
        "created_at": a["created_at"],
        "agent_id": a["agent_id"],
        "action": a["action"],
        "status": a["status"],
        "expires_at": a["expires_at"],
        "group_id": p.get("group_id") or p.get("feed"),
        "entries": len(p.get("quarantine_ids", [])),
        "root_cause": p.get("root_cause"),
        "confidence": p.get("confidence"),
    }


def create_app(connect: Callable[[], Any], allowed_hosts: list[str]) -> FastAPI:
    """`connect` opens a warehouse connection (one per request: the page is for a few
    people, and a dropped connection never outlives a request)."""
    app = FastAPI(title="dip approvals", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.connect = connect

    @contextmanager
    def unavailable() -> Iterator[None]:
        # Before any intake run has created the ops tables (and so applied this login's
        # grants), say so instead of a stack trace.
        import psycopg

        try:
            yield
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
            raise HTTPException(503, f"warehouse not ready: {exc.diag.message_primary}") from exc

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return files("dip.agents").joinpath("approvals.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/approvals")
    def list_approvals(conn: Conn, status: str = Query("pending", pattern=STATUS_PATTERN)
                       ) -> list[dict[str, Any]]:  # fmt: skip
        with unavailable():
            rows = approvals.pending(conn, None if status == "all" else status)
        return [_summary(a) for a in reversed(rows)]  # newest first

    @app.get("/api/approvals/{approval_id}")
    def show(approval_id: int, conn: Conn) -> dict[str, Any]:
        with unavailable(), conn.cursor() as cur:
            cur.execute(f"select {', '.join(DETAIL)} from ops.approvals where approval_id = %s",
                        (approval_id,))  # fmt: skip
            row = cur.fetchone()
        conn.commit()
        if row is None:
            raise HTTPException(404, f"no approval {approval_id}")
        a = dict(zip(DETAIL, row, strict=True))
        # A contract PR carries its unified diff against the current contract (Stage 5).
        a["diff"] = (a["payload"] or {}).get("diff") if a["action"] == "contract_pr" else None
        return a

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(approval_id: int, body: Decision, conn: Conn,
               x_reviewer: str | None = Header(default=None)) -> dict[str, Any]:  # fmt: skip
        if not x_reviewer or not REVIEWER.fullmatch(x_reviewer):
            raise HTTPException(400, "an X-Reviewer header (letters, digits, . _ -) is required")
        with unavailable():
            with conn.cursor() as cur:
                cur.execute("select session_user")
                (db_user,) = cur.fetchone()
            conn.commit()
            me = human_policy(f"{x_reviewer}@{db_user}")
            tool = f"approvals.{body.decision}"
            args = {"approval_id": approval_id}
            started = time.monotonic()
            try:
                status = approvals.decide(conn, approval_id, body.decision == "approve",
                                          me.agent, body.note)  # fmt: skip
            except approvals.ApprovalError as exc:
                # A refused decision is audited too (the CLI only prints it).
                Registry(conn).audit(me.agent, None, tool, "act", args, "denied", str(exc),
                                     None, int((time.monotonic() - started) * 1000))  # fmt: skip
                raise HTTPException(409, str(exc)) from exc
            # The same audit row the CLI writes for a decision.
            Registry(conn).audit(me.agent, None, tool, "act", args, "allowed", None, status,
                                 None)  # fmt: skip
        return {"approval_id": approval_id, "status": status, "decided_by": me.agent}

    @app.get("/api/audit")
    def audit(
        conn: Conn,
        principal: str | None = None,
        tool: str | None = None,
        decision: str | None = Query(None, pattern="^(allowed|denied)$"),
        since: datetime | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        where, params = [], []
        for column, value in (("principal", principal), ("tool", tool), ("decision", decision)):
            if value:
                where.append(f"{column} = %s")
                params.append(value)
        if since:
            where.append("at >= %s")
            params.append(since)
        sql = (f"select {', '.join(AUDIT)} from ops.audit_log"
               + (f" where {' and '.join(where)}" if where else "")
               + " order by audit_id desc limit %s")  # fmt: skip
        with unavailable(), conn.cursor() as cur:
            cur.execute(sql, (*params, limit))
            rows = [dict(zip(AUDIT, r, strict=True)) for r in cur.fetchall()]
        conn.commit()
        return rows

    return app
