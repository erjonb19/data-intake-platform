"""Drill-only DAG (Stage 11, S11.9; PLAN.md section 8.13): a task stuck in queued.

It ships in every deployment and stays paused: only `dip drill stuck_queued` unpauses and
triggers it (on the kind cluster), and `--undo` pauses it again and clears its run. Its one
task asks for more memory than the kind node has, so the executor's pod stays `Pending`
(`FailedScheduling`: Insufficient memory) and the task stays `queued`. The platform check
opens TASK.STUCK_QUEUED for `drill_queue.wait_for_room` before Airflow's own
`task_queued_timeout` (600 s) fails it, and runbook 12 says what to do.

Not a pool with 0 slots: that would leave the task `scheduled`, not `queued`, and on
`default_pool` it would stop every DAG.
"""

from __future__ import annotations

import pendulum
from airflow.sdk import dag, task


def _executor_config() -> dict:
    from kubernetes.client import models as k8s

    # 256Gi: far more than any kind node (a GitHub runner has 16 GB), so no node can take it
    return {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(
        name="base",
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "256Gi"}, limits={"memory": "256Gi"}),
    )]))}  # fmt: skip


@dag(
    dag_id="drill_queue",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["platform", "drill"],
    doc_md=__doc__,
)
def drill_queue():
    @task(executor_config=_executor_config(), retries=0)
    def wait_for_room() -> None:
        raise RuntimeError("this task found a node with 256Gi free: the drill did not take")

    wait_for_room()


drill_queue()
