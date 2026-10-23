"""The recorded demo (Stage 8, S8.2): one breakage through the whole platform, on a local
Postgres, with the model's turns replayed from recordings (no API key, no model call).

    bash scripts/demo.sh                  (starts a throwaway Postgres in Docker)
    python -m dip demo [--record]         (DIP_EVAL_DSN: a throwaway database)

1. A vendor's eligibility file arrives without its member_id column (`dip break`).
2. The intake quarantines it whole and opens a FILE.QUARANTINED incident.
3. The LangGraph chain: the incident agent diagnoses it, code hands the incident's
   quarantine to triage through the typed edge, and triage proposes a resend.
4. The triage agent tries to run its proposal before anyone approved it: refused.
5. A person approves it; the agent runs it.
6. The vendor's corrected file arrives: it loads, supersedes the quarantine, and the
   incident closes.
7. The audit trail of the whole chain.

`--record` calls the model once and saves its turns (agents/evals/demo/recordings/); the
replay then fails if the platform ever sends the model anything else.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from dip import BUCKET_LANDING
from dip.agents import approvals, evals, graph, triage
from dip.agents.llm import LLM, AnthropicLLM, Recorder, Recording
from dip.agents.registry import Registry
from dip.breakages import apply
from dip.contracts import REPO_ROOT
from dip.intake import process_landing
from dip.storage import LocalStore
from dip.warehouse import Warehouse

RECORDINGS = REPO_ROOT / "agents" / "evals" / "demo" / "recordings"
BASE = REPO_ROOT / "tests" / "fixtures" / "break_loop" / "elig_base.csv"
FEED = "healthcare.eligibility"
CHAIN = "demo"


def _q(conn: Any, sql: str, *params: Any) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        out = cur.fetchall()
    conn.commit()
    return out


def run(dsn: str, work: Path, record: bool = False, say: Any = print) -> int:
    import psycopg

    recorders: dict[str, Recorder] = {}

    def llm(name: str) -> LLM:
        if record:
            recorders[name] = Recorder(AnthropicLLM())
            return recorders[name]
        return Recording(RECORDINGS / f"{name}.json")

    def step(n: int, text: str) -> None:
        say(f"\n== {n}. {text}")

    shutil.rmtree(work, ignore_errors=True)  # a fresh store, as the warehouse is reset
    store = LocalStore(work)
    contracts = evals.eval_contracts()
    good = BASE.read_bytes()
    with psycopg.connect(dsn) as conn, psycopg.connect(dsn) as chain_conn:
        evals._reset(conn)
        step(1, "A vendor's eligibility file arrives without its member_id column")
        broken = apply(good, [{"op": "drop_column", "column": "member_id"}])
        store.put(BUCKET_LANDING, f"{FEED}/elig_20261021.csv", broken)
        [o] = process_landing(store, Warehouse(conn), contracts, evals.INGEST_AT)
        say(f"intake: {o.status} {list(o.reason_codes)}")
        step(2, "The intake opened an incident")
        [(iid, kind, detail)] = _q(conn, "select incident_id, kind, detail from ops.incident")
        say(f"incident {iid}: {kind} ({detail})")

        step(3, "The LangGraph chain: incident agent -> code -> triage agent")
        with evals.LocalServer(conn, store, agents=("incident", "triage")) as srv:
            chain = graph.Chain(
                conn=chain_conn,
                incident_client=lambda: triage.mcp_client(srv.url, srv.keys["incident"]),
                triage_client=lambda: triage.mcp_client(srv.url, srv.keys["triage"]),
                llm_for=lambda s: llm(f"demo_{s}"),
            )
            state = asyncio.run(graph.run_chain(chain, iid, CHAIN))
            d = state["diagnosis"] or {}
            say(f"diagnosis: {d.get('root_cause')} ({d.get('confidence')}): {d.get('summary')}")
            h = state["handoff"]
            say(
                f"handoff (decided in code): {list(h.group_ids) if h else state['handoff_refused']}"
            )
            proposals = (state["triage_run"] or {}).get("proposals", [])
            for p in proposals:
                say(f"proposal: approval {p['approval_id']} {p['action']} for {p['group_id']}")
            if not proposals:
                say("no proposal: the demo stops here")
                return 1
            aid = proposals[0]["approval_id"]

            async def act(name: str) -> triage.TriageRun:
                async with triage.mcp_client(srv.url, srv.keys["triage"]) as client:
                    return await triage.act(client, llm(name), aid, run_id=f"{CHAIN}/{name}")

            step(4, f"The triage agent tries to run approval {aid} before anyone approved it")
            r = asyncio.run(act("demo_act_denied"))
            say(f"agent: {r.summary}")
            step(5, f"A person approves approval {aid}; the agent runs it")
            approvals.decide(conn, aid, True, "human:demo@dip", "the vendor must resend")
            # As `dip approvals approve` does: a person's decision is audited too.
            Registry(conn).audit(
                "human:demo@dip",
                None,
                "approvals.approve",
                "act",
                {"approval_id": aid},
                "allowed",
                None,
                "approved",
                None,
            )
            say(f"approval {aid}: approved by human:demo@dip")
            r = asyncio.run(act("demo_act"))
            say(f"agent: {r.summary}")

        step(6, "The vendor's corrected file arrives")
        store.put(BUCKET_LANDING, f"{FEED}/elig_20261021_resend.csv", good)
        [o] = process_landing(
            store, Warehouse(conn), contracts, evals.INGEST_AT + timedelta(hours=1)
        )
        say(f"intake: {o.status}, {o.superseded} quarantine entries superseded")
        [(closed,)] = _q(
            conn, "select closed_at is not null from ops.incident where incident_id = %s", iid
        )
        say(f"incident {iid}: {'closed' if closed else 'still open'}")
        open_left = _q(conn, "select count(*) from ops.quarantine where status = 'open'")[0][0]

        step(7, "The audit trail")
        for run_id, principal, tool, decision, reason in _q(
            conn,
            "select run_id, principal, tool, decision, coalesce(reason, '')"
            " from ops.audit_log order by audit_id",
        ):
            say(f"  {run_id or '-':<22} {principal:<16} {tool:<27} {decision:<8} {reason[:60]}")
    if record:
        for name, rec in recorders.items():
            rec.save(RECORDINGS / f"{name}.json")
    ok = bool(h) and closed and open_left == 0
    say(f"\n{'demo complete' if ok else 'demo FAILED'}")
    return 0 if ok else 1
