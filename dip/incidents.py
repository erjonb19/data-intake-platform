"""Incidents raised by breakages (Stage 7, S7.1), next to the late-file ones (dip/sla.py).

`ops.incident` holds one row per feed, kind and window (a UTC day here; the scheduled time
for FEED.LATE). Kinds:

  FEED.LATE          the expected file did not arrive (dip/sla.py, the feed_sla DAG)
  FILE.QUARANTINED   a file of the feed was quarantined whole (the intake, as it happens).
                     The files are linked to the incident (ops.incident_file); it closes
                     when none of them has an open file-level quarantine entry left (a
                     resend superseded it, or a person's discard or replay closed it).
                     Other files' row rejections the same day do not hold it open
  DAG.FAILED         a run of the feed's intake DAG failed (its failure callback). It
                     closes at the DAG's next successful run

Platform incidents (Stage 11, S11.6; dip/platform_check.py) belong to no feed: their feed
is the reserved `_platform`, and `target` names what broke. Each closes when the check sees
it healthy again:

  PLATFORM.SCHEDULER_DOWN   the Airflow scheduler's heartbeat is stale (target: scheduler)
  TASK.OOMKILLED            a task's pod was killed for memory (target: <dag>.<task>)
  TASK.STUCK_QUEUED         a task waited in scheduled or queued too long (<dag>.<task>)

The incident agent diagnoses an open incident and reports to `ops.incident_diagnosis`
(dip/agents/incident_tools.py); the LangGraph chain hands quarantined records to triage
(dip/agents/graph.py). Windows use the database clock, as quarantine entries do.
"""

from __future__ import annotations

from typing import Any

KINDS = ("FEED.LATE", "FILE.QUARANTINED", "DAG.FAILED", "PLATFORM.SCHEDULER_DOWN",
         "TASK.OOMKILLED", "TASK.STUCK_QUEUED")  # fmt: skip
PLATFORM_KINDS = ("PLATFORM.SCHEDULER_DOWN", "TASK.OOMKILLED", "TASK.STUCK_QUEUED")
PLATFORM_FEED = "_platform"


def open_incident(cur: Any, feed: str, kind: str, detail: str, file_sha256: str | None = None,
                  target: str | None = None) -> int | None:  # fmt: skip
    """Open the feed's incident of this kind for today (UTC). One per day: while it is open,
    later ones add nothing but their file; a closed one opens again (a replayed file
    quarantined anew). `file_sha256` links the file that caused it (ops.incident_file).
    Returns the id when it was opened or reopened, else None."""
    if kind not in KINDS:
        raise ValueError(f"unknown incident kind {kind!r}")
    cur.execute(
        "insert into ops.incident (feed, kind, window_start, since, detail, target)"
        " values (%s, %s, date_trunc('day', now() at time zone 'UTC') at time zone 'UTC',"
        " now(), %s, %s) on conflict (feed, kind, window_start, (coalesce(target, '')))"
        " do update set closed_at = null, since = now(), opened_at = now(),"
        " detail = excluded.detail where ops.incident.closed_at is not null"
        " returning incident_id",
        (feed, kind, detail[:1000], target),
    )
    row = cur.fetchone()
    if file_sha256:
        cur.execute(
            "insert into ops.incident_file (incident_id, file_sha256)"
            " select incident_id, %s from ops.incident where feed = %s and kind = %s"
            "   and window_start = date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'"
            " on conflict do nothing",
            (file_sha256, feed, kind),
        )
    return row[0] if row else None


def close_quarantine_incidents(cur: Any, feed: str) -> int:
    """Close the feed's FILE.QUARANTINED incidents none of whose files still has an open
    file-level quarantine entry."""
    cur.execute(
        "update ops.incident i set closed_at = now()"
        " where i.feed = %s and i.kind = 'FILE.QUARANTINED' and i.closed_at is null"
        " and not exists (select 1 from ops.incident_file f join ops.quarantine q"
        "   on q.feed = i.feed and q.file_sha256 = f.file_sha256"
        "   where f.incident_id = i.incident_id and q.status = 'open' and q.level = 'file')",
        (feed,),
    )
    return cur.rowcount


def close_platform(cur: Any, kind: str, target: str) -> int:
    """Close the open platform incident of this kind and target (any day)."""
    cur.execute(
        "update ops.incident set closed_at = now() where feed = %s and kind = %s"
        " and coalesce(target, '') = %s and closed_at is null",
        (PLATFORM_FEED, kind, target),
    )
    return cur.rowcount


def close_dag_failed(cur: Any, feed: str) -> int:
    cur.execute(
        "update ops.incident set closed_at = now() where feed = %s and kind = 'DAG.FAILED'"
        " and closed_at is null",
        (feed,),
    )
    return cur.rowcount


def dag_callback(feed: str, failed: bool, context: dict[str, Any], connect: Any) -> None:
    """The feed DAG's run callbacks (dags/feed_intake.py): a failed run opens DAG.FAILED, a
    successful one closes it. `connect()` gives a warehouse connection."""
    run = context.get("dag_run")
    run_id = getattr(run, "run_id", "") or ""
    dag_id = getattr(run, "dag_id", "") or ""
    conn = connect()
    try:
        with conn.cursor() as cur:
            if failed:
                reason = str(context.get("reason") or "a task failed")
                open_incident(cur, feed, "DAG.FAILED", f"{dag_id} run {run_id}: {reason}")
            else:
                close_dag_failed(cur, feed)
        conn.commit()
    finally:
        conn.close()
