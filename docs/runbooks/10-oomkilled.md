# Runbook 10: a task's pod ran out of memory

**Signal.** An open `TASK.OOMKILLED` incident in `ops.incident` (feed `_platform`, target
`<dag_id>.<task_id>`), opened by the `platform-check` CronJob when a pod in `dip-airflow`
ended with its container `OOMKilled` (exit code 137: the kernel killed it at its memory
limit). Alert **TaskPodOOMKilled**; for a feed's DAG, `DAG.FAILED` and **DagRunFailing**
too. The task failed; nothing it was writing was committed.

**Break it on purpose (kind only):** `python -m dip drill oom_task` unpauses and triggers
the drill-only `drill_oom` DAG, whose task allocates far past its pod's 128Mi limit;
`python -m dip drill oom_task --undo` pauses it again and deletes its pods. The drill runs
in the GitHub proof workflow, never on the laptop.

## Evidence

1. `get_incident(incident_id)`: the target (which DAG and task) and the pod's limits.
2. `get_k8s_events(pod=<dag id, as the pod name spells it>)`: the termination (`OOMKilled`,
   exit 137, `limits.memory`) and when.
3. `get_dag_run(dag_id)` and `get_task_log`: the task's last lines before the kill (often
   none: the process is killed without a traceback, and the log ends mid-way).

## Decide

A task OOMKilled, out of memory at its limit: what to do, by what ran and how often.

| What you see | Action |
|---|---|
| One large file, the task reads it whole (a large Excel workbook, a big FHIR bundle) | Ask the vendor to split it, or raise the worker pods' memory limit (`workers.kubernetes.resources` in `infra/helm-values/airflow.yaml`) and re-apply with Terraform |
| Every run of the task is killed, with ordinary inputs | Raise that limit, or give the task its own in its DAG's `executor_config` `pod_override` (as `drill_oom` sets 128Mi), within the node's room; then rerun the failed task |
| The drill (`drill_oom`) | Expected: `dip drill oom_task --undo` |
| Memory grows run after run (a leak in our code) | Open an issue with the task and the pod's last limits; do not just raise the limit |

## Check

Rerun the failed task, then delete its failed pod, which Airflow keeps
(`delete_worker_pods_on_failure` is False): `kubectl -n dip-airflow delete pod -l
dag_id=<dag_id>,task_id=<task_id>`. The next `platform-check` run (within 2 minutes) finds
no OOMKilled pod left for that task and closes the incident; **TaskPodOOMKilled** clears.
