"""Warehouse metrics for Prometheus (Stage 3, S3.2): `python -m dip metrics-serve`.

A small exporter over the ops tables, scraped by Prometheus every 30 s. Every value is
computed at scrape time from fixed queries (no labels from free text but feed names, reason
codes and test names, all from contracts and the dbt project), so the series stay few:

  dip_intake_files{feed,status}                files ever recorded, by outcome
  dip_files_24h{feed,status}                   the same for files received in the last 24 h
  dip_rows_24h{feed,outcome}                   accepted / rejected rows, last 24 h
  dip_feed_last_received_timestamp_seconds{feed}  freshness: time() minus this
  dip_quarantine_open{feed,reason_code}        quarantine rows waiting for a person
  dip_dq_test_status{test_name,dq_category,status}  1 for each test's latest result
  dip_dq_test_failures{test_name}              failing rows in that latest result
  dip_incidents_open{feed,kind}                open incidents (FEED.LATE)
  dip_agent_runs_24h{agent,status}             agent runs, last 24 h (Stage 11, S11.4): a
                                               model step's (ok / failed) or a chain's
  dip_agent_fallback_runs_24h{agent}           step runs answered by the free fallback
  dip_approvals_decided_7d{agent,decision}     proposals approved, rejected or expired
  dip_model_spend_usd_today{provider}          metered spend today (UTC), live calls
  dip_model_calls_24h{agent,provider}          live model calls, last 24 h
  dip_model_latency_p95_seconds_24h{agent}     p95 model call latency, last 24 h
  dip_airflow_scheduler_heartbeat_age_seconds  the scheduler's last heartbeat, as the
                                               platform check saw it (S11.6)
  dip_metrics_query_ok{query}                  1, or 0 when a query failed (a table not
                                               created yet, a missing grant)

It connects as `dip_metrics`, a login that can read only the columns these queries name
(column grants made where each table is created) and nothing in bronze, silver or gold.
Read-only and short statements by role settings. Works with psycopg 3 (dev) or psycopg2
(the Airflow image).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# Stage 11 (S11.4): a model step's logical runs (an attempt and its restarts on the
# fallback, `<run id>@<provider>`), live calls of the last 24 hours. An attempt failed if a
# call in it errored (a failed call ends the attempt); the run failed if its last attempt
# did, and ran on the fallback if any attempt was answered by Gemini.
STEP_RUNS = (
    "attempts as (select step, split_part(run_id, '@', 1) as run, bool_or(status <> 'ok')"
    " as failed, bool_or(provider = 'gemini') as gemini, min(started_at) as t, run_id"
    " from ops.model_call where source = 'live' and step is not null"
    " and started_at > now() - interval '24 hours' group by step, run_id),"
    # on a tie in time, `<run>@<provider>` (the restart) sorts after `<run>`
    " step_runs as (select step, (array_agg(failed order by t desc, run_id desc))[1] as failed,"
    " bool_or(gemini) as on_fallback from attempts group by step, run)"
)

# name -> (help, type, sql). Each query returns label columns then one value column.
QUERIES: dict[str, tuple[str, str, str]] = {
    "dip_intake_files": (
        "Files recorded by the intake, by outcome.",
        "gauge",
        "select feed, status, count(*) from ops.intake_file group by 1, 2",
    ),
    "dip_files_24h": (
        "Files received in the last 24 hours, by outcome.",
        "gauge",
        "select feed, status, count(*) from ops.intake_file"
        " where received_at > now() - interval '24 hours' group by 1, 2",
    ),
    "dip_rows_24h": (
        "Rows accepted and rejected in files received in the last 24 hours.",
        "gauge",
        "select feed, 'accepted', sum(accepted_count) from ops.intake_file"
        " where received_at > now() - interval '24 hours' group by 1"
        " union all select feed, 'rejected', sum(rejected_count) from ops.intake_file"
        " where received_at > now() - interval '24 hours' group by 1",
    ),
    "dip_feed_last_received_timestamp_seconds": (
        "When the feed's newest file was received (Unix time).",
        "gauge",
        # Deliveries, as the late-file check counts them: a full-file feed that resends the
        # same content under a new name has one intake_file row but a new delivery.
        "select feed, extract(epoch from max(received_at)) from ops.intake_delivery group by 1",
    ),
    "dip_quarantine_open": (
        "Quarantine rows waiting for a person.",
        "gauge",
        "select feed, reason_code, count(*) from ops.quarantine where status = 'open'"
        " group by 1, 2",
    ),
    "dip_dq_test_status": (
        "1 for each dbt test's latest result.",
        "gauge",
        "select test_name, dq_category, status, 1 from ("
        " select distinct on (test_name) test_name, dq_category, status from ops.dq_results"
        " order by test_name, recorded_at desc) latest",
    ),
    "dip_dq_test_failures": (
        "Failing rows in each dbt test's latest result.",
        "gauge",
        "select test_name, coalesce(failures, 0) from ("
        " select distinct on (test_name) test_name, failures from ops.dq_results"
        " order by test_name, recorded_at desc) latest",
    ),
    "dip_incidents_open": (
        "Open incidents, such as FEED.LATE.",
        "gauge",
        "select feed, kind, count(*) from ops.incident where closed_at is null group by 1, 2",
    ),
    # Stage 11 (S11.4): the agents. `agent` is a model step (incident, triage, profile, map,
    # review: its runs from ops.model_call, live calls only) or a whole chain (incident_chain
    # from ops.agent_run, intake_team from ops.intake_job, by their own finished statuses;
    # running, queued and skipped are not outcomes yet). A step's run is one logical run with
    # its restarts on the fallback (`<run id>@gemini`): it failed if its last attempt did, and
    # it ran on the fallback if any attempt was answered by Gemini. Labels come from fixed
    # lists, never free text.
    "dip_agent_runs_24h": (
        "Agent runs started in the last 24 hours, by agent and outcome.",
        "gauge",
        f"with {STEP_RUNS} select agent, status, count(*) from ("
        " select step as agent, case when failed then 'failed' else 'ok' end as status"
        " from step_runs"
        " union all select 'incident_chain', status from ops.agent_run"
        " where started_at > now() - interval '24 hours'"
        " and status not in ('running', 'skipped')"
        " union all select 'intake_team', status from ops.intake_job"
        " where created_at > now() - interval '24 hours'"
        " and status not in ('queued', 'running')) runs group by 1, 2",
    ),
    "dip_agent_fallback_runs_24h": (
        "Agent step runs in the last 24 hours answered by the free fallback provider.",
        "gauge",
        f"with {STEP_RUNS} select step, count(*) from step_runs where on_fallback group by 1",
    ),
    "dip_approvals_decided_7d": (
        "Proposals decided (or expired) in the last 7 days, by agent and decision.",
        "gauge",
        # executed and failed (an execution that failed) were both approved by a person
        "select agent_id, case when status in ('executed', 'failed') then 'approved'"
        " else status end,"
        " count(*) from ops.approvals where status in ('approved', 'executed', 'rejected',"
        " 'expired', 'failed') and coalesce(decided_at, expires_at) > now() - interval '7 days'"
        " group by 1, 2",
    ),
    "dip_model_spend_usd_today": (
        "Metered model spend today (UTC), live calls, by provider (dip.agents.llm.cost_usd).",
        "gauge",
        "select provider, sum(cost_usd) from ops.model_call where source = 'live'"
        " and started_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC'"
        " group by 1",
    ),
    "dip_model_calls_24h": (
        "Live model calls in the last 24 hours, by agent step and provider.",
        "gauge",
        "select step, provider, count(*) from ops.model_call where source = 'live'"
        " and step is not null and started_at > now() - interval '24 hours' group by 1, 2",
    ),
    "dip_model_latency_p95_seconds_24h": (
        "95th percentile latency of live model calls in the last 24 hours, by agent step.",
        "gauge",
        "select step, percentile_cont(0.95) within group (order by latency_ms) / 1000.0"
        " from ops.model_call where source = 'live' and step is not null"
        " and started_at > now() - interval '24 hours' group by 1",
    ),
    # Stage 11 (S11.6): the scheduler's heartbeat as the platform check last saw it, aged
    # to now (it keeps growing while the scheduler, or the check itself, is down).
    "dip_airflow_scheduler_heartbeat_age_seconds": (
        "Seconds since the Airflow scheduler's last heartbeat seen by the platform check.",
        "gauge",
        "select extract(epoch from now() - observed) from ops.platform_check"
        " where check_name = 'scheduler_heartbeat' and observed is not null"
        " order by at desc limit 1",
    ),
}
LABELS: dict[str, tuple[str, ...]] = {
    "dip_intake_files": ("feed", "status"),
    "dip_files_24h": ("feed", "status"),
    "dip_rows_24h": ("feed", "outcome"),
    "dip_feed_last_received_timestamp_seconds": ("feed",),
    "dip_quarantine_open": ("feed", "reason_code"),
    "dip_dq_test_status": ("test_name", "dq_category", "status"),
    "dip_dq_test_failures": ("test_name",),
    "dip_incidents_open": ("feed", "kind"),
    "dip_agent_runs_24h": ("agent", "status"),
    "dip_agent_fallback_runs_24h": ("agent",),
    "dip_approvals_decided_7d": ("agent", "decision"),
    "dip_model_spend_usd_today": ("provider",),
    "dip_model_calls_24h": ("agent", "provider"),
    "dip_model_latency_p95_seconds_24h": ("agent",),
    "dip_airflow_scheduler_heartbeat_age_seconds": (),
}


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: Any) -> str:
    return "0" if value is None else repr(float(value))


def render(run: Callable[[str], Iterable[tuple]]) -> str:
    """The exposition text: every query's series, plus whether each query worked. `run`
    executes one SQL statement and returns its rows; a failure marks that query 0 and
    leaves its series out, so one missing table never hides the rest."""
    out: list[str] = []
    ok: dict[str, int] = {}
    for name, (help_text, kind, sql) in QUERIES.items():
        try:
            rows = list(run(sql))
        except Exception:  # noqa: BLE001 - reported as dip_metrics_query_ok 0
            ok[name] = 0
            continue
        ok[name] = 1
        out += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
        for row in rows:
            *labels, value = row
            pairs = ",".join(
                f'{k}="{_escape(v)}"' for k, v in zip(LABELS[name], labels, strict=True)
            )
            out.append(f"{name}{{{pairs}}} {_number(value)}")
    out += ["# HELP dip_metrics_query_ok 1 if the query ran.", "# TYPE dip_metrics_query_ok gauge"]
    out += [f'dip_metrics_query_ok{{query="{n}"}} {v}' for n, v in ok.items()]
    return "\n".join(out) + "\n"


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError:  # the Airflow image ships psycopg2
        import psycopg2 as psycopg  # type: ignore[no-redef]
    conn = psycopg.connect(dsn, connect_timeout=3)  # never hold the scrape on a dead server
    conn.autocommit = True
    return conn


class _Runner:
    """One connection, reopened after an error (a restarted Postgres, a dropped link)."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None

    def __call__(self, sql: str) -> list[tuple]:
        if self.conn is None or self.conn.closed:
            self.conn = _connect(self.dsn)
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(sql)
                return cur.fetchall()
            finally:
                cur.close()
        except Exception:
            if getattr(self.conn, "closed", True):
                self.conn = None
            raise


def serve(port: int, dsn: str | None = None) -> None:
    runner = _Runner(dsn or os.environ["DIP_METRICS_DSN"])

    class Handler(BaseHTTPRequestHandler):
        # One request at a time, so a client that connects and sends nothing must not
        # hold the server: its socket times out and the next scrape is served.
        timeout = 10

        def do_GET(self):  # noqa: N802 - http.server's name
            if self.path not in ("/metrics", "/healthz"):
                self.send_error(404)
                return
            body = b"ok\n" if self.path == "/healthz" else render(runner).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # scrapes every 30 s: keep the log quiet
            pass

    print(f"dip metrics on :{port}/metrics", flush=True)
    # One request at a time: the scrapes share one connection, and come every 30 s.
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 - in a pod
