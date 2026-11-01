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

The exporter turns the latest heartbeat into dip_airflow_scheduler_heartbeat_age_seconds,
and the AirflowSchedulerDown alert fires above 180 s.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from dip.incidents import PLATFORM_FEED, close_platform, open_incident


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
        if t["reason"] == "OOMKilled":
            target = f"{t['dag_id'] or '?'}.{t['task_id'] or t['pod']}"
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


def run(conn: Any, services: Any, k8s: Any = None, now: datetime | None = None,
        env: dict[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    env = dict(os.environ) if env is None else env
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        out |= check_scheduler(cur, services, now,
                               float(env.get("DIP_SCHEDULER_STALE_SECONDS", "90")))  # fmt: skip
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
