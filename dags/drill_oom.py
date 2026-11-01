"""Drill-only DAG (Stage 11, S11.7; PLAN.md section 8.13): a task that runs out of memory.

It ships in every deployment and stays paused: only `dip drill oom_task` unpauses and
triggers it (on the kind cluster), and `--undo` pauses it again. Its one task reads more
than its pod's 128Mi memory limit allows, so the kernel kills the container (OOMKilled,
exit 137) and the task fails: the platform check opens TASK.OOMKILLED for
`drill_oom.eat_memory`, and runbook 10 says what to do.
"""

from __future__ import annotations

import pendulum
from airflow.sdk import dag, task


def _executor_config() -> dict:
    from kubernetes.client import models as k8s

    return {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(containers=[k8s.V1Container(
        name="base",
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "64Mi"}, limits={"memory": "128Mi"}),
    )]))}  # fmt: skip


@dag(
    dag_id="drill_oom",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["platform", "drill"],
    doc_md=__doc__,
)
def drill_oom():
    @task(executor_config=_executor_config(), retries=0)
    def eat_memory() -> None:
        hold = []
        for _ in range(64):  # 64 x 8 MiB = 512 MiB, four times the limit
            hold.append(bytearray(8 * 1024 * 1024))
        raise RuntimeError("still alive past the memory limit: the drill did not take")

    eat_memory()


drill_oom()
