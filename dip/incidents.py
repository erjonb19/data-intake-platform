"""Incidents raised by breakages (Stage 7, S7.1), next to the late-file ones (dip/sla.py).

`ops.incident` holds one row per feed, kind and window (a UTC day here; the scheduled time
for FEED.LATE). Kinds:

  FEED.LATE          the expected file did not arrive (dip/sla.py, the feed_sla DAG)
  FILE.QUARANTINED   a file of the feed was quarantined whole (the intake, as it happens).
                     It closes when no quarantine of the feed opened that day is still open
                     (a resend superseded it, or a person's discard or replay closed it)
  DAG.FAILED         a run of the feed's intake DAG failed (its failure callback). It
                     closes at the DAG's next successful run

The incident agent diagnoses an open incident and reports to `ops.incident_diagnosis`
(dip/agents/incident_tools.py); the LangGraph chain hands quarantined records to triage
(dip/agents/graph.py). Windows use the database clock, as quarantine entries do.
"""

from __future__ import annotations

from typing import Any

KINDS = ("FEED.LATE", "FILE.QUARANTINED", "DAG.FAILED")


def open_incident(cur: Any, feed: str, kind: str, detail: str) -> int | None:
    """Open the feed's incident of this kind for today (UTC). One per day: while it is open,
    later ones add nothing; a closed one opens again (a replayed file quarantined anew).
    Returns its id when it was opened or reopened, else None."""
    if kind not in KINDS:
        raise ValueError(f"unknown incident kind {kind!r}")
    cur.execute(
        "insert into ops.incident (feed, kind, window_start, since, detail)"
        " values (%s, %s, date_trunc('day', now() at time zone 'UTC') at time zone 'UTC',"
        " now(), %s) on conflict (feed, kind, window_start) do update"
        " set closed_at = null, since = now(), detail = excluded.detail"
        " where ops.incident.closed_at is not null returning incident_id",
        (feed, kind, detail[:1000]),
    )
    row = cur.fetchone()
    return row[0] if row else None


def close_quarantine_incidents(cur: Any, feed: str) -> int:
    """Close the feed's FILE.QUARANTINED incidents whose day has no open quarantine left."""
    cur.execute(
        "update ops.incident i set closed_at = now()"
        " where i.feed = %s and i.kind = 'FILE.QUARANTINED' and i.closed_at is null"
        " and not exists (select 1 from ops.quarantine q where q.feed = i.feed"
        "   and q.status = 'open' and q.created_at >= i.window_start"
        "   and q.created_at < i.window_start + interval '1 day')",
        (feed,),
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
