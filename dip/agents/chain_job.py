"""The incident -> triage chain as an unattended Airflow workload (Stage 9, S9.1).

Until now the chain only ran when a person typed `dip incident run --incident ID`. The
`agent_chain` DAG (dags/agent_chain.py) runs this module on a schedule: it claims open
incidents nobody has run the chain on, runs the chain on each, and records the outcome in
`ops.agent_run`. The claim row is inserted before the chain starts, so two DAG runs can
never start the chain on the same incident, and an incident the chain failed on is not
retried by the schedule (a person clears or re-runs it).

Nothing here widens what the agents may do: they still reach the platform only through
the MCP server with their own keys and policies, and nothing executes without an
approval row a person decided. What this adds is the trigger.

The cluster never needs a paid key to be healthy: with no model key in the DAG's
environment every claimed incident is recorded `skipped: no model key` and the task
exits 0. The decision is in `plan()` and tested without a cluster.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MODEL_KEYS = (("ANTHROPIC_API_KEY", "anthropic"), ("GEMINI_API_KEY", "gemini"))
MCP_VARS = ("DIP_MCP_URL", "DIP_INCIDENT_KEY", "DIP_TRIAGE_KEY")


@dataclass(frozen=True)
class Plan:
    """Whether the chain can run in this environment, and with which model."""

    runnable: bool
    provider: str | None = None
    reason: str | None = None


def plan(env: dict[str, str] | None = None) -> Plan:
    env = os.environ if env is None else env
    missing = [v for v in MCP_VARS if not env.get(v)]
    if missing:
        return Plan(False, reason=f"no MCP configuration: {', '.join(missing)} unset")
    for var, provider in MODEL_KEYS:
        if env.get(var):
            return Plan(True, provider=provider)
    return Plan(
        False, reason="no model key (ANTHROPIC_API_KEY or GEMINI_API_KEY) in the agent environment"
    )


def claim(conn: Any, limit: int = 3, dag_run_id: str | None = None) -> list[tuple[int, str, str]]:
    """Open incidents with no agent_run row, claimed as `running` in one statement.

    `for update skip locked` on the incident rows means two task instances claiming at
    once get disjoint sets; the primary key on agent_run is the backstop."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with todo as (
                select i.incident_id
                from ops.incident i
                left join ops.agent_run r on r.incident_id = i.incident_id
                where i.closed_at is null and r.incident_id is null
                order by i.opened_at
                limit %s
                for update of i skip locked
            )
            insert into ops.agent_run (incident_id, status, dag_run_id)
            select incident_id, 'running', %s from todo
            on conflict do nothing
            returning incident_id
            """,
            (limit, dag_run_id),
        )
        ids = [int(r[0]) for r in cur.fetchall()]
        claimed: list[tuple[int, str, str]] = []
        if ids:
            cur.execute(
                "select incident_id, feed, kind from ops.incident where incident_id = any(%s)"
                " order by opened_at",
                (ids,),
            )
            claimed = [(int(r[0]), r[1], r[2]) for r in cur.fetchall()]
    conn.commit()
    return claimed


def finish(
    conn: Any,
    incident_id: int,
    status: str,
    reason: str | None = None,
    chain_id: str | None = None,
    root_cause: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update ops.agent_run set status = %s, reason = %s, chain_id = %s,"
            " root_cause = %s, finished_at = now() where incident_id = %s",
            (status, (reason or None) and reason[:2000], chain_id, root_cause, incident_id),
        )
    conn.commit()


def _llm_for(provider: str) -> Callable[[str], Any]:
    if provider == "gemini":
        from dip.agents.gemini import GeminiLLM

        return lambda step: GeminiLLM()
    from dip.agents.llm import AnthropicLLM

    return lambda step: AnthropicLLM()


def run_chain(conn: Any, incident_id: int, env: dict[str, str], provider: str) -> dict[str, Any]:
    """The same chain `dip incident run` runs, with the DAG's connection and keys."""
    import asyncio

    from dip.agents import graph, triage

    url = env["DIP_MCP_URL"]
    chain = graph.Chain(
        conn=conn,
        incident_client=lambda: triage.mcp_client(url, env["DIP_INCIDENT_KEY"]),
        triage_client=lambda: triage.mcp_client(url, env["DIP_TRIAGE_KEY"]),
        llm_for=_llm_for(provider),
    )
    return asyncio.run(graph.run_chain(chain, incident_id))


def run(
    conn: Any,
    env: dict[str, str] | None = None,
    limit: int = 3,
    dag_run_id: str | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One DAG task run: claim, run or skip each, record. Returns a summary.

    `runner` stands in for `run_chain` in tests. Never raises for a chain failure: that is
    recorded on the incident's row, and the task reports it in the summary."""
    env = dict(os.environ) if env is None else env
    p = plan(env)
    claimed = claim(conn, limit=limit, dag_run_id=dag_run_id)
    summary: dict[str, Any] = {
        "claimed": len(claimed),
        "done": [],
        "failed": [],
        "skipped": [],
        "plan": p.reason or p.provider,
    }
    for incident_id, _feed, _kind in claimed:
        if not p.runnable:
            finish(conn, incident_id, "skipped", reason=p.reason)
            summary["skipped"].append(incident_id)
            continue
        try:
            state = (runner or run_chain)(conn, incident_id, env, p.provider)
        except Exception as exc:  # the chain's own errors are the record, not a crash
            finish(
                conn,
                incident_id,
                "failed",
                reason=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
            )
            summary["failed"].append(incident_id)
            continue
        d = state.get("diagnosis") or {}
        finish(
            conn,
            incident_id,
            "done",
            chain_id=state.get("chain_id"),
            root_cause=d.get("root_cause"),
            reason=state.get("handoff_refused"),
        )
        summary["done"].append(incident_id)
    return summary
