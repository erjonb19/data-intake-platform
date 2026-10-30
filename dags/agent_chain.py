"""The incident agent as a workload (Stage 9, S9.1): every 5 minutes, claim the open
incidents the chain has not run on and run the incident -> triage chain on each
(dip/agents/chain_job.py), as its own pod with resource limits. Nothing a person did not
approve executes: the chain ends in pending proposals, as it does from the CLI.

The task runs in the platform image (the agents' dependencies are in it) with the
`dip-agent-chain` Secret as its environment: the in-cluster MCP URL, the incident and
triage agents' keys, and, when a person has set one, a model key. Without a model key
every claimed incident is recorded `skipped` in ops.agent_run and the task still
succeeds: the cluster never needs a paid key to be healthy. Like every DAG here it
starts paused."""

from __future__ import annotations

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
    @task(executor_config=_executor_config())
    def run_chain() -> dict:
        import os

        from airflow.sdk import get_current_context

        from dip.agents import chain_job

        try:
            run_id = get_current_context()["dag_run"].run_id
        except Exception:
            run_id = None
        conn = PostgresHook(postgres_conn_id="warehouse").get_conn()
        try:
            summary = chain_job.run(conn, env=dict(os.environ), limit=3, dag_run_id=run_id)
        finally:
            conn.close()
        print(
            f"claimed {summary['claimed']}: done {summary['done']}, failed"
            f" {summary['failed']}, skipped {summary['skipped']} ({summary['plan']})"
        )
        return summary

    run_chain()


agent_chain()
