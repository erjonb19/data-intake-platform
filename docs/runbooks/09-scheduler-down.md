# Runbook 9: the Airflow scheduler is down

**Signal.** An open `PLATFORM.SCHEDULER_DOWN` incident in `ops.incident` (feed `_platform`,
target `scheduler`), opened by the `platform-check` CronJob when the scheduler's last
heartbeat in Airflow's `/api/v2/monitor/health` is older than 90 seconds, or its status is
not healthy. Alert **AirflowSchedulerDown** (the heartbeat older than 3 minutes, from
`dip_airflow_scheduler_heartbeat_age_seconds`). The DAG runs dashboard's "Scheduler
heartbeat" panel drops to zero. Nothing is scheduled while it lasts: no intake, no dbt, no
SLA checks, no agent chain. Files wait in landing; nothing is lost.

**Break it on purpose (kind only):** `python -m dip drill scheduler_down` scales the
`airflow-scheduler` Deployment in `dip-airflow` to 0; `python -m dip drill scheduler_down
--undo` scales it back to 1. The drill runs in the GitHub proof workflow, never on the
laptop.

## Evidence

1. `get_incident(incident_id)`: the target and the last heartbeat the check saw.
2. `get_k8s_events(pod="scheduler")`: the scheduler pod's events and how its container
   ended (scaled down, evicted, `OOMKilled`, `CrashLoopBackOff`, an image pull error).
3. `get_dag_run` for any DAG: runs stuck in `queued`, or no new runs since the heartbeat
   stopped.

## Decide

The scheduler down, its heartbeat stale: what to do, by what the scheduler pod's
events show.

| What you see | Action |
|---|---|
| The Deployment was scaled to 0 (events: `ScalingReplicaSet`, the pod `Killing`) | Scale it back: `kubectl -n dip-airflow scale deployment airflow-scheduler --replicas=1` (or `dip drill scheduler_down --undo` after a drill) |
| The container was `OOMKilled` or is in `CrashLoopBackOff` | Read its last log (`kubectl -n dip-airflow logs deploy/airflow-scheduler --previous`); raise its memory limit in `infra/helm-values/airflow.yaml` and re-apply with Terraform |
| The pod cannot start (`ImagePullBackOff`, `FailedScheduling`) | Fix the image tag or free node room, then let the Deployment roll |
| The scheduler runs but cannot reach the metadata database | Check Postgres and the `airflow-metadata` Secret (runbook 11 for a rotated password) |

## Check

The next `platform-check` run (within 2 minutes) sees a fresh heartbeat and closes the
incident (`ops.incident.closed_at`); **AirflowSchedulerDown** clears; queued runs start.
Late files that waited in landing are picked up by their next scheduled intake run.
