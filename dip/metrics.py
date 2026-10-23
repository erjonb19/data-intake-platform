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
        "select feed, extract(epoch from max(received_at)) from ops.intake_file group by 1",
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
    conn = psycopg.connect(dsn)
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
