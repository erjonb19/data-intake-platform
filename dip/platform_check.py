"""Platform health, watched from outside Airflow (Stage 11, S11.6; PLAN.md section 8.13,
design choice 1).

    python -m dip platform-check      (the CronJob runs python -m dip.platform_check)

Run every 2 minutes by the `platform-check` CronJob (infra/terraform/platform_check.tf), in
the platform image, under its own service account (a namespace-scoped Role: get and list
pods and events in `dip-airflow`), its own warehouse login (`dip_platform_check`: incidents
and its own table only) and the Airflow Viewer login. A scheduler that is down cannot run a
DAG that notices it, so this is not a DAG.

Each check writes one `ops.platform_check` row (what it saw, as numbers and times) and
opens or closes a platform incident (dip/incidents.py, feed `_platform`):

  scheduler   the Airflow API's /api/v2/monitor/health (public): a heartbeat older than
              DIP_SCHEDULER_STALE_SECONDS (default 90), or none, opens
              PLATFORM.SCHEDULER_DOWN (target `scheduler`); a fresh one closes it.
              If the API itself does not answer, the check records that and decides
              nothing (the API server is down, not necessarily the scheduler)

  oomkilled   (S11.7) pods in dip-airflow whose container ended OOMKilled, by the Airflow
              task they ran (KubernetesExecutor labels them with dag_id and task_id; a
              failed task's pod is kept, delete_worker_pods_on_failure is False): each
              opens TASK.OOMKILLED (target `<dag_id>.<task_id>`). A failed pod is kept
              until a person deletes it (runbook 10's Check), so the incident stays open
              until then; a target with no such pod left closes

  dag_runs    (S11.8) each feed intake DAG's latest run (by run_after), from the Airflow
              API with the Viewer login: failed opens the feed's DAG.FAILED (target the
              DAG), success closes it, and a run still queued or running (a new one, or a
              failed one cleared to rerun) decides nothing. The run's own failure callback
              does the same, but it needs the warehouse connection, which is what a bad
              secret can break; this check has its own login

  stuck_queued (S11.9) task instances still `scheduled` or `queued` (the Airflow API, the
              Viewer login) longer than DIP_QUEUED_STALE_SECONDS (default 300, under
              Airflow's own scheduler.task_queued_timeout of 600 s, so the check sees a
              stuck task before Airflow fails it): each opens TASK.STUCK_QUEUED (target
              `<dag_id>.<task_id>`); a target with no such task left closes. A paused
              DAG's `scheduled` tasks are not stuck (the scheduler holds them on purpose;
              with more than 100 paused DAGs, no `scheduled` task is counted).
              The API cannot order by waiting time, so with more than 100 waiting tasks
              some may go unseen, and the check then closes nothing

The exporter turns the latest heartbeat into dip_airflow_scheduler_heartbeat_age_seconds,
and the AirflowSchedulerDown alert fires above 180 s. TaskPodOOMKilled and TaskQueuedTooLong
fire on the open incidents (dip_incidents_open).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from dip.incidents import PLATFORM_FEED, close_dag_failed, close_platform, open_incident


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)  # Airflow's times are UTC


def record(cur: Any, check: str, target: str, ok: bool, observed: datetime | None = None,
           value: float | None = None, detail: str = "") -> None:  # fmt: skip
    cur.execute(
        "insert into ops.platform_check (check_name, target, ok, observed, value, detail)"
        " values (%s, %s, %s, %s, %s, %s)",
        (check, target, ok, observed, value, detail[:500]),
    )


def _open_platform(cur: Any, kind: str, target: str, detail: str) -> None:
    """Open the incident unless one is already open for this kind and target, on any day: a
    fault that lasts past midnight (UTC) stays one incident, not one per day."""
    cur.execute("select 1 from ops.incident where feed = %s and kind = %s and target = %s"
                " and closed_at is null", (PLATFORM_FEED, kind, target))  # fmt: skip
    if cur.fetchone() is None:
        open_incident(cur, PLATFORM_FEED, kind, detail, target=target)


def check_scheduler(cur: Any, services: Any, now: datetime, stale_after: float) -> dict:
    try:
        health = services.get(services.airflow_url, "Airflow", "/api/v2/monitor/health")
    except Exception as exc:  # noqa: BLE001 - recorded, and no incident is decided on it
        record(cur, "airflow_api", "api-server", False, detail=f"{type(exc).__name__}: {exc}")
        return {"scheduler": "unknown (the Airflow API did not answer)"}
    s = (health or {}).get("scheduler") or {}
    beat = _time(s.get("latest_scheduler_heartbeat"))
    age = (now - beat).total_seconds() if beat else None
    # The age alone decides: Airflow calls the scheduler unhealthy after its own 30 s
    # (scheduler_health_check_threshold), stricter than this check's threshold; its status
    # is recorded, not acted on.
    healthy = age is not None and age <= stale_after
    record(cur, "scheduler_heartbeat", "scheduler", healthy, beat, age,
           f"status={s.get('status')}")  # fmt: skip
    if healthy:
        close_platform(cur, "PLATFORM.SCHEDULER_DOWN", "scheduler")
        return {"scheduler": "healthy"}
    seen = f"{age:.0f} s ago" if age is not None else "never"
    _open_platform(cur, "PLATFORM.SCHEDULER_DOWN", "scheduler",
                   f"the Airflow scheduler's last heartbeat was {seen}"
                   f" (status {s.get('status')!r}; stale after {stale_after:.0f} s)")  # fmt: skip
    return {"scheduler": f"down (last heartbeat {seen})"}


def check_oomkilled(cur: Any, k8s: Any) -> dict:
    from dip.k8s import terminations

    try:
        ended = terminations(k8s.pods())
    except Exception as exc:  # noqa: BLE001 - recorded; no incident is decided on it
        record(cur, "k8s_api", "dip-airflow", False, detail=f"{type(exc).__name__}: {exc}")
        return {"oomkilled": "unknown (the Kubernetes API did not answer)"}
    killed: dict[str, dict] = {}
    for t in ended:
        # Airflow task pods only (labelled by the executor): a long-running pod (the
        # scheduler, say) keeps an old OOMKilled in lastState after a healthy restart
        if t["reason"] == "OOMKilled" and t["dag_id"] and t["task_id"]:
            target = f"{t['dag_id']}.{t['task_id']}"
            killed.setdefault(target, t)
    for target, t in sorted(killed.items()):
        record(cur, "oomkilled", target, False, _time(t["finished_at"]),
               detail=f"pod {t['pod']} exit {t['exit_code']} limits {t['limits']}")  # fmt: skip
        _open_platform(cur, "TASK.OOMKILLED", target,
                       f"task {target}: pod {t['pod']} was OOMKilled (exit {t['exit_code']},"
                       f" limits {t['limits']})")  # fmt: skip
    cur.execute("select target from ops.incident where feed = %s and kind = 'TASK.OOMKILLED'"
                " and closed_at is null", (PLATFORM_FEED,))  # fmt: skip
    for (target,) in cur.fetchall():
        if target not in killed:
            close_platform(cur, "TASK.OOMKILLED", target)
    return {"oomkilled": sorted(killed)}


def check_dag_runs(cur: Any, services: Any) -> dict:
    try:
        # newest first, at most 100 (Airflow's default maximum_page_limit)
        got = services.airflow("/dags/~/dagRuns?order_by=-run_after&limit=100")
    except Exception as exc:  # noqa: BLE001 - recorded; no incident is decided on it
        record(cur, "airflow_api", "dag-runs", False, detail=f"{type(exc).__name__}: {exc}")
        return {"dag_runs": "unknown (the Airflow API did not answer)"}
    latest: dict[str, dict] = {}
    for r in (got or {}).get("dag_runs") or []:
        if str(r.get("dag_id", "")).startswith("intake__"):  # feed DAGs (dag_id_for)
            latest.setdefault(r["dag_id"], r)  # newest first
    failed = []
    for dag_id, r in sorted(latest.items()):
        feed = dag_id.removeprefix("intake__").replace("__", ".")
        if r.get("state") == "success":
            close_dag_failed(cur, feed)
        if r.get("state") != "failed":
            continue
        failed.append(dag_id)
        record(cur, "dag_run", dag_id, False, _time(r.get("end_date")),
               detail=f"run {r.get('dag_run_id')} failed")  # fmt: skip
        # the callback's incident, if it could open one (same feed, kind and target)
        cur.execute("select 1 from ops.incident where feed = %s and kind = 'DAG.FAILED'"
                    " and closed_at is null", (feed,))  # fmt: skip
        if cur.fetchone() is None:
            open_incident(cur, feed, "DAG.FAILED",
                          f"{dag_id} run {r.get('dag_run_id')}: failed (the platform check)",
                          target=dag_id)  # fmt: skip
    return {"dag_runs_failed": failed}


def check_stuck_queued(cur: Any, services: Any, now: datetime, stale_after: float) -> dict:
    try:
        # every DAG and run (~), at most 100 waiting task instances
        got = services.airflow("/dags/~/dagRuns/~/taskInstances?state=scheduled&state=queued"
                               "&limit=100")  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - recorded; no incident is decided on it
        record(cur, "airflow_api", "task-instances", False, detail=f"{type(exc).__name__}: {exc}")
        return {"stuck_queued": "unknown (the Airflow API did not answer)"}
    try:
        got_paused = services.airflow("/dags?paused=true&limit=100") or {}
        paused = {d.get("dag_id") for d in got_paused.get("dags") or []}
        # more paused DAGs than one page: a scheduled task may belong to one not listed
        paused_all = got_paused.get("total_entries", len(paused)) <= len(paused)
    except Exception as exc:  # noqa: BLE001 - recorded; no incident is decided on it
        record(cur, "airflow_api", "dags", False, detail=f"{type(exc).__name__}: {exc}")
        return {"stuck_queued": "unknown (the Airflow API did not answer)"}
    stuck: dict[str, tuple[float, datetime, dict]] = {}
    for ti in (got or {}).get("task_instances") or []:
        state = ti.get("state")
        if state not in ("scheduled", "queued"):
            continue
        if state == "scheduled" and (ti.get("dag_id") in paused or not paused_all):
            continue  # held by the pause, not stuck (or, past one page, perhaps so)
        since = _time(ti.get("queued_when") if state == "queued" else ti.get("scheduled_when"))
        waited = (now - since).total_seconds() if since else 0.0
        target = f"{ti.get('dag_id')}.{ti.get('task_id')}"
        if waited > max(stale_after, stuck.get(target, (0.0,))[0]):  # the longest wait
            stuck[target] = (waited, since, ti)
    for target, (waited, since, ti) in sorted(stuck.items()):
        what = (f"{ti.get('state')} for {waited:.0f} s in run {ti.get('dag_run_id')}"
                f" (pool {ti.get('pool')})")  # fmt: skip
        record(cur, "stuck_queued", target, False, since, waited, what)
        _open_platform(cur, "TASK.STUCK_QUEUED", target,
                       f"task {target}: {what}; stale after {stale_after:.0f} s")  # fmt: skip
    tis = (got or {}).get("task_instances") or []
    if (got or {}).get("total_entries", len(tis)) > len(tis):
        return {"stuck_queued": sorted(stuck)}  # more than one page waits: close nothing
    cur.execute("select target from ops.incident where feed = %s and kind = 'TASK.STUCK_QUEUED'"
                " and closed_at is null", (PLATFORM_FEED,))  # fmt: skip
    for (target,) in cur.fetchall():
        if target not in stuck:
            close_platform(cur, "TASK.STUCK_QUEUED", target)
    return {"stuck_queued": sorted(stuck)}


def run(conn: Any, services: Any, k8s: Any = None, now: datetime | None = None,
        env: dict[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    env = dict(os.environ) if env is None else env
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        out |= check_scheduler(cur, services, now,
                               float(env.get("DIP_SCHEDULER_STALE_SECONDS", "90")))  # fmt: skip
        out |= check_dag_runs(cur, services)
        out |= check_stuck_queued(cur, services, now,
                                  float(env.get("DIP_QUEUED_STALE_SECONDS", "300")))  # fmt: skip
        if k8s is not None and (k8s.base or k8s.http):
            out |= check_oomkilled(cur, k8s)
    conn.commit()
    return out


def main() -> int:
    import json

    from dip.agents.read_tools import ReadServices
    from dip.k8s import K8s
    from dip.metrics import _connect

    conn = _connect(os.environ["DIP_WAREHOUSE_DSN"])
    conn.autocommit = False
    try:
        print(json.dumps(run(conn, ReadServices.from_env(), K8s.from_env())))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
