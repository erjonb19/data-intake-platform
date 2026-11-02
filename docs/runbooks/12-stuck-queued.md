# Runbook 12: a task stuck in queued

**Signal.** An open `TASK.STUCK_QUEUED` incident in `ops.incident` (feed `_platform`, target
`<dag_id>.<task_id>`), opened by the `platform-check` CronJob when a task instance stayed
`scheduled` or `queued` longer than its threshold (`DIP_QUEUED_STALE_SECONDS`, 300 s).
Alert **TaskQueuedTooLong**. The task has not started, so nothing it would write exists
yet. Left alone, Airflow's own `scheduler.task_queued_timeout` (600 s) requeues it and, after
its retries, fails it.

**Break it on purpose (kind only):** `python -m dip drill stuck_queued` lowers the check's
threshold to 120 s and triggers the drill-only `drill_queue` DAG, whose task asks for 256Gi
of memory, so its pod can never be scheduled; `python -m dip drill stuck_queued --undo`
pauses the DAG, deletes its run and pod, restarts the scheduler and restores the threshold.
The drill runs in the GitHub proof workflow, never on the laptop.

## Evidence

1. `get_incident(incident_id)`: the target (which DAG and task), the state it waits in
   (`queued` or `scheduled`), for how long, and its pool.
2. `get_k8s_events(pod=<dag id, as the pod name spells it>)`: for a `queued` task, its pod's
   `FailedScheduling` events say why no node takes it (`Insufficient memory`,
   `Insufficient cpu`, an untolerated taint). No pod and no event at all: the executor has
   not started one (its slots are full, `core.parallelism`).
3. `get_dag_run(dag_id)`: the run and its tasks' states (the stuck task `queued`, nothing
   after it started).

## Decide

A task stuck in queued or scheduled, waiting to run: what to do, by what its pod's events
and its state show.

| What you see | Action |
|---|---|
| `queued`, its pod `Pending` with `FailedScheduling: Insufficient memory` (or cpu): it asks for more than any node has free | Lower the request in its DAG's `executor_config` `pod_override`, or the worker pods' default (`workers.kubernetes.resources` in `infra/helm-values/airflow.yaml`), within the node's room, and re-apply with Terraform; then clear the task |
| `queued`, `FailedScheduling` because the node is full of other pods (no single pod is too large) | Wait for the running tasks to finish, or stop what is not needed; add node capacity if it keeps happening |
| `scheduled`, no pod: the pool or the executor has no free slot | Check the pool's slots (`airflow pools list`) and `core.parallelism`; give the pool slots back or wait for running tasks |
| The drill (`drill_queue`) | Expected: `dip drill stuck_queued --undo` |

## Check

Once the task starts (or after `--undo` for the drill), the next `platform-check` run
(within 2 minutes) finds no task of that target waiting past the threshold and closes the
incident; **TaskQueuedTooLong** clears. A task that waited past `task_queued_timeout` was
failed or requeued by Airflow: clear it to run it again.
