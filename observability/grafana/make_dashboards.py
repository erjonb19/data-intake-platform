"""Writes the Grafana dashboards (python observability/grafana/make_dashboards.py).

Two dashboards over Prometheus: feeds (files, quarantine and rejection rates, freshness,
open quarantine, DQ results, late incidents) and DAG runs (Airflow's StatsD metrics). The
JSON is generated so the panels stay consistent; edit this file, not the JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
DS = {"type": "prometheus", "uid": "prometheus"}
FEEDS_DESC = "Files, quarantine, freshness and data quality per feed (ops tables via dip-metrics)."
DAGS_DESC = "Airflow DAG and task metrics from StatsD (the Airflow chart's statsd-exporter)."


def panel(pid, title, kind, exprs, x, y, w=12, h=8, unit=None, legend=None, desc=""):
    targets = [
        {"refId": chr(65 + i), "datasource": DS, "expr": e,
         "legendFormat": (legend or [""] * len(exprs))[i],
         **({"instant": True, "format": "table"} if kind == "table" else {})}
        for i, e in enumerate(exprs)
    ]  # fmt: skip
    p = {
        "id": pid,
        "title": title,
        "description": desc,
        "type": kind,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
        "options": {},
    }
    if kind == "table":
        p["transformations"] = [{"id": "merge", "options": {}}]
    return p


def dashboard(uid, title, panels, desc):
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["data-intake-platform"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "editable": False,
        "panels": panels,
    }


def feeds():
    return dashboard("dip-feeds", "Feeds", [
        panel(1, "Files received (24 h) by outcome", "timeseries",
              ["sum by (feed, status) (dip_files_24h)"], 0, 0,
              legend=["{{feed}} {{status}}"]),
        panel(2, "Files quarantined whole (24 h)", "timeseries",
              ['sum by (feed) (dip_files_24h{status="quarantined"})'
               " / sum by (feed) (dip_files_24h)"], 12, 0, unit="percentunit",
              legend=["{{feed}}"], desc="Alert QuarantineRateHigh fires over 20%."),
        panel(3, "Rows rejected (24 h)", "timeseries",
              ['sum by (feed) (dip_rows_24h{outcome="rejected"}) / sum by (feed) (dip_rows_24h)'],
              0, 8, unit="percentunit", legend=["{{feed}}"],
              desc="Alert RowRejectionRateHigh fires over 10%."),
        panel(4, "Freshness: time since the newest file", "timeseries",
              ["time() - dip_feed_last_received_timestamp_seconds"], 12, 8, unit="s",
              legend=["{{feed}}"]),
        panel(5, "Open late-file incidents (FEED.LATE)", "stat",
              ['sum(dip_incidents_open{kind="FEED.LATE"}) or vector(0)'], 0, 16, w=6),
        panel(6, "Open quarantine rows by reason", "table",
              ["sum by (feed, reason_code) (dip_quarantine_open)"], 6, 16, w=18),
        panel(7, "dbt tests: latest result", "table",
              ["dip_dq_test_status", "dip_dq_test_failures"], 0, 24, w=24, h=10,
              desc="Alert DataQualityError fires on error or fail; warnings are findings."),
    ], FEEDS_DESC)  # fmt: skip


def dag_runs():
    return dashboard("dip-dag-runs", "DAG runs", [
        panel(1, "Failed DAG runs (1 h)", "timeseries",
              ["sum by (dag_id) (increase(airflow_dagrun_failed_count[1h]))"], 0, 0,
              legend=["{{dag_id}}"], desc="Alert DagRunFailing fires above 0."),
        panel(2, "Successful run duration (p50)", "timeseries",
              ['airflow_dagrun_duration{quantile="0.5"}'], 12, 0, unit="ms",
              legend=["{{dag_id}}"]),
        panel(3, "Task successes and failures (1 h)", "timeseries",
              ["sum(increase(airflow_ti_successes[1h]))",
               "sum(increase(airflow_ti_failures[1h]))"], 0, 8,
              legend=["succeeded", "failed"]),
        panel(4, "Scheduler heartbeat (per min)", "timeseries",
              ["rate(airflow_scheduler_heartbeat[5m]) * 60"], 12, 8, legend=["heartbeat"]),
        panel(5, "Scrape targets up", "table", ["up"], 0, 16, w=24, h=6),
    ], DAGS_DESC)  # fmt: skip


if __name__ == "__main__":
    for name, d in (("feeds.json", feeds()), ("dag-runs.json", dag_runs())):
        (HERE / name).write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {name}")
