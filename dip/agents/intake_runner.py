"""Intake jobs as an unattended Airflow workload (Stage 10, S10.4; PLAN.md section 8.12).

`python -m dip.agents.intake_runner`, from the agent_chain DAG's `intake_jobs` task in the
agent venv: claims queued intake jobs one at a time (dip/intake_jobs.py) and runs the intake
team on each (dip/agents/intake_graph.py), recording the outcome on the job's row: proposed
(with its approval id), no_change (the map worker declined), or failed (with the reason).

Like the incident chain (dip/agents/chain_job.py), nothing here widens what the agents may
do, and the cluster never needs a paid key: without a model key, or without the team's MCP
keys, nothing is claimed and the jobs wait. If every provider is down before anything was
written, the job goes back to the queue.
"""

from __future__ import annotations

import os
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from dip.agents.chain_job import _llm_for, merge_version, set_versions
from dip.agents.llm import NoProvider, provider_names

KEYS = {
    "profile": "DIP_PROFILE_KEY",
    "map": "DIP_AUTHOR_KEY",
    "validator": "DIP_VALIDATOR_KEY",
    "reviewer": "DIP_REVIEWER_KEY",
    "orchestrator": "DIP_ORCHESTRATOR_KEY",
}


def plan(env: dict[str, str]) -> tuple[tuple[str, ...], str | None]:
    """(providers, None) when jobs can run here, else ((), why not)."""
    missing = [v for v in ("DIP_MCP_URL", *KEYS.values()) if not env.get(v)]
    if missing:
        return (), f"no intake-team MCP configuration: {', '.join(missing)} unset"
    names = provider_names(env)
    if not names:
        return (), "no model key (ANTHROPIC_API_KEY or GEMINI_API_KEY) in the agent environment"
    return tuple(names), None


def requeue(conn: Any, job_id: int, reason: str, dag_run_id: str | None = None) -> bool:
    """Back to the queue: only the claim this run made (like finish)."""
    with conn.cursor() as cur:
        cur.execute(
            "update ops.intake_job set status = 'queued', claimed_at = null, reason = %s"
            " where job_id = %s and status = 'running'"
            " and (%s::text is null or dag_run_id = %s::text)",
            (reason[:2000], job_id, dag_run_id, dag_run_id),
        )
        updated = cur.rowcount == 1
    conn.commit()
    return updated


def run_job(
    conn: Any, job: dict[str, Any], env: dict[str, str], providers: tuple[str, ...]
) -> dict[str, Any]:
    import asyncio

    from dip.agents import intake_graph, triage

    chain_id = f"intake-{uuid.uuid4().hex[:10]}"
    with conn.cursor() as cur:
        cur.execute(
            # A new chain, and no profile from an earlier run of the job: this run's profile
            # worker must report its own.
            "update ops.intake_job set chain_id = %s, profile = null, review = null,"
            " versions = null where job_id = %s",
            (chain_id, job["job_id"]),
        )
    conn.commit()
    url = env["DIP_MCP_URL"]
    team = intake_graph.IntakeTeam(
        conn=conn,
        client_for=lambda member: triage.mcp_client(url, env[KEYS[member]]),
        llm_for=_llm_for(providers),
        on_version=lambda step, v: merge_version(
            conn, "intake_job", "job_id", job["job_id"], step, v
        ),  # fmt: skip
    )
    return dict(asyncio.run(intake_graph.run_intake(team, job, chain_id)))


def run(
    conn: Any,
    env: dict[str, str] | None = None,
    limit: int = 1,
    dag_run_id: str | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from dip.intake_jobs import claim_one, finish

    env = dict(os.environ) if env is None else env
    providers, why = plan(env)
    summary: dict[str, Any] = {"claimed": 0, "done": {}, "plan": why or " > ".join(providers)}
    if why:
        return summary
    for _ in range(limit):
        job = claim_one(conn, dag_run_id)
        if job is None:
            break
        summary["claimed"] += 1
        try:
            state = (runner or run_job)(conn, job, env, providers)
        except NoProvider as exc:
            conn.rollback()
            if exc.step == "profile":  # nothing written: the job waits for a provider
                requeue(conn, job["job_id"], f"no provider answered: {exc}", dag_run_id)
                summary["done"][job["job_id"]] = "queued"
                break
            finish(
                conn, job["job_id"], "failed", reason=f"NoProvider: {exc}", dag_run_id=dag_run_id
            )
            summary["done"][job["job_id"]] = "failed"
            continue
        except Exception as exc:  # the team's errors are the record, not a crash
            conn.rollback()
            finish(
                conn,
                job["job_id"],
                "failed",
                reason=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                dag_run_id=dag_run_id,
            )
            summary["done"][job["job_id"]] = "failed"
            continue
        status = state.get("outcome") or "failed"
        provider = " ".join(f"{k}:{v}" for k, v in (state.get("providers") or {}).items())
        finish(
            conn,
            job["job_id"],
            status,
            reason=state.get("reason"),
            chain_id=state.get("chain_id"),
            provider=provider or None,
            approval_id=state.get("approval_id"),
            dag_run_id=dag_run_id,
        )
        set_versions(conn, "intake_job", "job_id", job["job_id"], state.get("versions"))
        summary["done"][job["job_id"]] = status
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    import psycopg

    from dip.agents import gemini, intake_graph, llm, triage  # noqa: F401  (the import check)

    ap = argparse.ArgumentParser(prog="python -m dip.agents.intake_runner")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--dag-run-id")
    args = ap.parse_args(argv)
    from dip.agents import model_calls

    dsn = os.environ["DIP_WAREHOUSE_DSN"]
    with psycopg.connect(dsn) as conn, model_calls.to_warehouse(dsn):  # S11.1
        summary = run(conn, limit=args.limit, dag_run_id=args.dag_run_id)
    summary["spent_usd"] = round(llm.SPENT["usd"], 6)  # Claude's responses, by their usage
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
