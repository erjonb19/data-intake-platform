"""The incident -> triage chain as an unattended Airflow workload (Stage 9, S9.1).

Until now the chain only ran when a person typed `dip incident run --incident ID`. The
`agent_chain` DAG (dags/agent_chain.py) runs this module on a schedule: it claims open
incidents nobody has run the chain on, runs the chain on each, and records the outcome in
`ops.agent_run`. Incidents are claimed one at a time, and the claim row is written
before the chain starts, so two DAG runs can never start the chain on the same incident
and a pod that dies strands at most one claim; a claim still `running` after
`STALE_AFTER` (longer than the task's execution timeout) is taken back. An incident the
chain failed on is not retried by the schedule (a person clears or re-runs it).

Nothing here widens what the agents may do: they still reach the platform only through
the MCP server with their own keys and policies, and nothing executes without an
approval row a person decided. What this adds is the trigger.

The cluster never needs a paid key to be healthy: with no model key in the DAG's
environment the task claims nothing, reports `skipped: no model key` and exits 0, so the
incidents wait for a key instead of being used up. (Rows recorded `skipped` by an earlier
version are claimed again.) The decision is in `plan()` and tested without a cluster.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from dip.agents.llm import NoProvider

MCP_VARS = ("DIP_MCP_URL", "DIP_INCIDENT_KEY", "DIP_TRIAGE_KEY")
# A claim older than this is a dead pod's: dags/agent_chain.py's execution timeout is
# shorter, so a live task never has its claim taken back.
STALE_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class Plan:
    """Whether the chain can run in this environment, and with which model."""

    runnable: bool
    providers: tuple[str, ...] = ()  # in the order they are tried (Stage 10, S10.1)
    reason: str | None = None


def plan(env: dict[str, str] | None = None) -> Plan:
    env = os.environ if env is None else env
    missing = [v for v in MCP_VARS if not env.get(v)]
    if missing:
        return Plan(False, reason=f"no MCP configuration: {', '.join(missing)} unset")
    from dip.agents.llm import provider_names

    names = provider_names(env)
    if names:
        return Plan(True, providers=tuple(names))
    return Plan(
        False, reason="no model key (ANTHROPIC_API_KEY or GEMINI_API_KEY) in the agent environment"
    )


def claim_one(conn: Any, dag_run_id: str | None = None) -> tuple[int, str, str] | None:
    """The oldest open incident the chain has not finished, claimed as `running`.

    Eligible: no agent_run row, a `skipped` one, or a `running` one older than
    STALE_AFTER. `for update skip locked` on the incident row means two task instances
    claiming at once get different incidents; the upsert's own condition is the backstop."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with todo as (
                select i.incident_id
                from ops.incident i
                left join ops.agent_run r on r.incident_id = i.incident_id
                where i.closed_at is null
                  and (r.incident_id is null or r.status = 'skipped'
                       or (r.status = 'running' and r.started_at < now() - %(stale)s))
                order by i.opened_at, i.incident_id
                limit 1
                for update of i skip locked
            )
            insert into ops.agent_run (incident_id, status, dag_run_id)
            select incident_id, 'running', %(run)s from todo
            on conflict (incident_id) do update
                set status = 'running', started_at = now(), finished_at = null,
                    reason = null, chain_id = null, root_cause = null, provider = null,
                    dag_run_id = excluded.dag_run_id
                where ops.agent_run.status = 'skipped'
                   or (ops.agent_run.status = 'running'
                       and ops.agent_run.started_at < now() - %(stale)s)
            returning incident_id
            """,
            {"stale": STALE_AFTER, "run": dag_run_id},
        )
        row = cur.fetchone()
        claimed = None
        if row:
            cur.execute(
                "select incident_id, feed, kind from ops.incident where incident_id = %s",
                (row[0],),
            )
            i, feed, kind = cur.fetchone()
            claimed = (int(i), feed, kind)
    conn.commit()
    return claimed


def claim(conn: Any, limit: int = 3, dag_run_id: str | None = None) -> list[tuple[int, str, str]]:
    """Up to `limit` incidents, each claimed on its own (see claim_one)."""
    out = []
    while len(out) < limit and (c := claim_one(conn, dag_run_id)):
        out.append(c)
    return out


def finish(
    conn: Any,
    incident_id: int,
    status: str,
    reason: str | None = None,
    chain_id: str | None = None,
    root_cause: str | None = None,
    provider: str | None = None,
) -> None:
    """`provider`: which provider answered each step, as `incident:anthropic
    triage:gemini` (S10.1), so a fallback run is visible on the row and the approvals page."""
    with conn.cursor() as cur:
        cur.execute(
            "update ops.agent_run set status = %s, reason = %s,"
            " chain_id = coalesce(%s, chain_id),"
            " root_cause = %s, provider = coalesce(%s, provider), finished_at = now()"
            " where incident_id = %s",
            (
                status,
                (reason or None) and reason[:2000],
                chain_id,
                root_cause,
                provider,
                incident_id,
            ),  # fmt: skip
        )
    conn.commit()


def set_versions(conn: Any, table: str, key: str, value: int, versions: dict | None) -> None:
    """Stage 11 (S11.3): each step's provider, model and prompt hash on the run's row."""
    import json

    if not versions or table not in ("agent_run", "intake_job") or key not in (
            "incident_id", "job_id"):  # fmt: skip
        return
    with conn.cursor() as cur:
        cur.execute(f"update ops.{table} set versions = %s::jsonb where {key} = %s",
                    (json.dumps(versions, sort_keys=True), value))  # fmt: skip
    conn.commit()


def merge_version(conn: Any, table: str, key: str, value: int, step: str,
                  version: dict[str, str]) -> None:  # fmt: skip
    """Stage 11 (S11.3): one finished step's version, on the run's row at once, like its
    provider: a chain that fails later keeps the versions of the steps that ran."""
    import json

    if table not in ("agent_run", "intake_job") or key not in ("incident_id", "job_id"):
        return
    with conn.cursor() as cur:
        cur.execute(f"update ops.{table} set versions = coalesce(versions, '{{}}'::jsonb)"
                    f" || jsonb_build_object(%s::text, %s::jsonb) where {key} = %s",
                    (step, json.dumps(version, sort_keys=True), value))  # fmt: skip
    conn.commit()


def record_step(conn: Any, incident_id: int, step: str, provider: str) -> None:
    """A finished step's provider, on the incident's row at once (S10.1): a proposal made
    on the fallback is labelled while the chain runs on, and if it later fails."""
    with conn.cursor() as cur:
        cur.execute(
            "update ops.agent_run set provider = concat_ws(' ', provider, %s::text)"
            " where incident_id = %s",
            (f"{step}:{provider}", incident_id),
        )
    conn.commit()


def _llm_for(providers: tuple[str, ...]) -> Callable[[str], Any]:
    """Every step tries the providers in order, restarting on the next while it has
    written nothing (dip.agents.llm.Failover)."""
    from dip.agents.llm import FACTORIES, Failover

    return lambda step: Failover([(n, FACTORIES[n]) for n in providers])


def run_chain(conn: Any, incident_id: int, env: dict[str, str], providers: tuple[str, ...]
              ) -> dict[str, Any]:  # fmt: skip
    """The same chain `dip incident run` runs, with the DAG's connection and keys."""
    import asyncio
    import uuid

    from dip.agents import graph, triage

    # The chain id goes on the row before the chain starts, with each step's provider as
    # it finishes (S10.1): the approvals page finds a running chain's proposals by it.
    chain_id = f"chain-{uuid.uuid4().hex[:10]}"
    with conn.cursor() as cur:
        cur.execute("update ops.agent_run set chain_id = %s where incident_id = %s",
                    (chain_id, incident_id))  # fmt: skip
    conn.commit()
    url = env["DIP_MCP_URL"]
    chain = graph.Chain(
        conn=conn,
        incident_client=lambda: triage.mcp_client(url, env["DIP_INCIDENT_KEY"]),
        triage_client=lambda: triage.mcp_client(url, env["DIP_TRIAGE_KEY"]),
        llm_for=_llm_for(providers),
        on_step=lambda step, provider: record_step(conn, incident_id, step, provider),
        on_version=lambda step, v: merge_version(
            conn, "agent_run", "incident_id", incident_id, step, v
        ),  # fmt: skip
    )
    return asyncio.run(graph.run_chain(chain, incident_id, chain_id))


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
    summary: dict[str, Any] = {
        "claimed": 0,
        "done": [],
        "failed": [],
        "plan": p.reason or " > ".join(p.providers),
    }
    if not p.runnable:  # claim nothing: the incidents wait for a configured environment
        return summary
    for _ in range(limit):
        claimed = claim_one(conn, dag_run_id)
        if claimed is None:
            break
        incident_id = claimed[0]
        summary["claimed"] += 1
        try:
            state = (runner or run_chain)(conn, incident_id, env, p.providers)
        except NoProvider as exc:
            conn.rollback()
            if exc.step == "incident":  # nothing written: the incident waits, as with no key
                finish(conn, incident_id, "skipped", reason=f"no provider answered: {exc}")
                summary["skipped"] = [*summary.get("skipped", []), incident_id]
                break  # every provider is down: stop this run; the next one tries again
            finish(conn, incident_id, "failed", reason=f"NoProvider: {exc}")
            summary["failed"].append(incident_id)
            continue
        except Exception as exc:  # the chain's own errors are the record, not a crash
            conn.rollback()  # a failed statement in the chain leaves the transaction aborted
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
            provider=" ".join(f"{k}:{v}" for k, v in (state.get("providers") or {}).items())
            or None,
        )
        set_versions(conn, "agent_run", "incident_id", incident_id, state.get("versions"))
        summary["done"].append(incident_id)
    return summary


def main(argv: list[str] | None = None) -> int:
    """`python -m dip.agents.chain_job`: what the agent_chain task runs, in the Airflow
    image's agent venv (the agents' dependencies are kept out of Airflow's own). The
    warehouse DSN comes from DIP_WAREHOUSE_DSN. The chain's modules are imported first on
    every run, key or not, so a run without a key still proves the venv can run the chain."""
    import argparse
    import json

    import psycopg

    from dip.agents import gemini, graph, llm, triage  # noqa: F401  (the import check)

    ap = argparse.ArgumentParser(prog="python -m dip.agents.chain_job")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--dag-run-id")
    args = ap.parse_args(argv)
    from dip.agents import model_calls

    dsn = os.environ["DIP_WAREHOUSE_DSN"]
    with psycopg.connect(dsn) as conn, model_calls.to_warehouse(dsn):  # S11.1
        summary = run(conn, limit=args.limit, dag_run_id=args.dag_run_id)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
