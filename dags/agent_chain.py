"""The incident agent as a workload (Stage 9, S9.1): every 5 minutes, claim the open
incidents the chain has not run on and run the incident -> triage chain on each
(dip/agents/chain_job.py), as its own pod with resource limits. Nothing a person did not
approve executes: the chain ends in pending proposals, as it does from the CLI.

The task runs in the platform image (the agents' dependencies are in it) with the
`dip-agent-chain` Secret as its environment: the in-cluster MCP URL, the incident and
triage agents' keys, and, when a person has set one, a model key. Without a model key
the task claims nothing and still succeeds: the cluster never needs a paid key to be
healthy, and the incidents wait for one. Like every DAG here it starts paused.

A second task, `intake_jobs` (Stage 10, S10.2), turns new vendors' samples under
landing/_onboard/<pack>.<name>/ into queued onboarding jobs for the intake team."""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task


def _executor_config() -> dict:
    """The task's pod: the agent environment from the Secret (optional, so the DAG is
    valid before Terraform has created it) and limits an agent run cannot exceed."""
    from kubernetes.client import models as k8s

    return {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(
        name="base",
        env_from=[k8s.V1EnvFromSource(
            secret_ref=k8s.V1SecretEnvSource(name="dip-agent-chain", optional=True))],
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "1", "memory": "512Mi"}),
    )]))}  # fmt: skip


@dag(
    dag_id="agent_chain",
    schedule="*/5 * * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["agents", "incident"],
    doc_md=__doc__,
)
def agent_chain():
    # Shorter than chain_job.STALE_AFTER, so a live task's claim is never taken back; a
    # hung MCP or model call fails the task instead of holding max_active_runs forever.
    @task(executor_config=_executor_config(), execution_timeout=timedelta(minutes=20))
    def run_chain() -> dict:
        import json
        import os
        import subprocess

        from airflow.sdk import get_current_context

        from dip.dbt_runner import connection_dsn
        from dip.warehouse import Warehouse

        try:
            run_id = get_current_context()["dag_run"].run_id
        except Exception:
            run_id = None
        hook = PostgresHook(postgres_conn_id="warehouse")
        conn = hook.get_conn()
        try:
            Warehouse(conn).init()  # ops.agent_run, before any intake run created it
        finally:
            conn.close()
        # The chain runs in the agent venv: its dependencies (the Anthropic SDK, the MCP
        # client, LangGraph) are kept out of Airflow's, as dbt's are.
        python = os.environ.get("DIP_AGENT_PYTHON", "/opt/airflow/agent-venv/bin/python")
        cmd = [python, "-m", "dip.agents.chain_job", "--limit", "3"]
        if run_id:
            cmd += ["--dag-run-id", run_id]
        env = {**os.environ, "DIP_WAREHOUSE_DSN": connection_dsn(hook.get_connection("warehouse"))}
        p = subprocess.run(cmd, env=env, capture_output=True, text=True)
        print(p.stdout[-4000:], p.stderr[-4000:], sep="\n")
        if p.returncode != 0:
            raise RuntimeError(f"chain_job exited {p.returncode}")
        summary = json.loads(p.stdout.strip().splitlines()[-1])
        print(
            f"claimed {summary['claimed']}: done {summary['done']}, failed"
            f" {summary['failed']} ({summary['plan']})"
        )
        return summary

    # Stage 10 (S10.2): new vendors' samples under landing/_onboard/ become samples and
    # queued onboarding jobs (dip/intake_jobs.py). Runs in Airflow's own environment (it
    # needs MinIO and the warehouse, no model). The intake graph that claims the jobs is
    # S10.4; without a model key it will claim nothing, as the chain does.
    @task(executor_config=_executor_config(), execution_timeout=timedelta(minutes=10))
    def intake_jobs() -> dict:
        from airflow.providers.amazon.aws.hooks.s3 import S3Hook

        from dip.contracts import load_contracts
        from dip.intake_jobs import scan_onboarding
        from dip.storage import S3Store
        from dip.warehouse import Warehouse

        store = S3Store(S3Hook(aws_conn_id="minio").get_conn())
        conn = PostgresHook(postgres_conn_id="warehouse").get_conn()
        try:
            Warehouse(conn).init()  # ops.intake_job, before any intake run created it
            out = scan_onboarding(conn, store, load_contracts())
        finally:
            conn.close()
        for line in out["refused"]:
            print(f"not taken: {line}")
        print(f"onboarding: queued {out['queued']}, feed already open {out['already_open']}")
        return out

    run_chain()
    intake_jobs()


agent_chain()
