"""Stage 3 (S3.2) end-to-end check of the observability profile, after the file-feed cases.

Through port-forwards (scripts/e2e.sh): Prometheus scrapes all three targets, has the
warehouse metrics for the feeds the E2E just ran and Airflow's StatsD metrics, and has
loaded the alert rules; Grafana is up, refuses an anonymous API call, and has both
provisioned dashboards on its Prometheus data source.

Env: PROMETHEUS_URL, GRAFANA_URL, GRAFANA_PASSWORD.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROM = os.environ["PROMETHEUS_URL"]
GRAFANA = os.environ["GRAFANA_URL"]
AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['GRAFANA_PASSWORD']}".encode()).decode()


def get(url: str, auth: str | None = None) -> tuple[int, dict | list]:
    req = urllib.request.Request(url, headers={"Authorization": auth} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def query(expr: str) -> list[dict]:
    _, body = get(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    return body.get("data", {}).get("result", []) if isinstance(body, dict) else []


def wait_for(what: str, check, seconds: int = 180) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            print(f"==> {what}: ok")
            return True
        time.sleep(5)
    print(f"FAIL: {what}", file=sys.stderr)
    return False


def main() -> int:
    ok = all([
        wait_for("Prometheus scrapes prometheus, airflow-statsd and dip-metrics", lambda: {
            r["metric"]["job"] for r in query("up == 1")
        } >= {"prometheus", "airflow-statsd", "dip-metrics"}),
        wait_for("every warehouse metrics query runs as dip_metrics", lambda: (
            (q := query("dip_metrics_query_ok")) and all(r["value"][1] == "1" for r in q))),
        wait_for("warehouse metrics for the E2E's feeds", lambda: {
            r["metric"]["feed"] for r in query("dip_intake_files")
        } >= {"healthcare.eligibility", "healthcare.claims", "finance.vendor_invoice"}),
        wait_for("Airflow StatsD metrics (DAG run durations)", lambda: bool(
            query("airflow_dagrun_duration_count"))),
        wait_for("the alert rules are loaded", lambda: len([
            r for g in get(f"{PROM}/api/v1/rules")[1].get("data", {}).get("groups", [])
            for r in g["rules"]]) >= 10),
        wait_for("Grafana is up", lambda: get(f"{GRAFANA}/api/health")[0] == 200),
        wait_for("Grafana refuses an anonymous API call", lambda: get(
            f"{GRAFANA}/api/search")[0] == 401, 10),
        wait_for("Grafana has the three dashboards", lambda: {
            d["uid"] for d in get(f"{GRAFANA}/api/search?type=dash-db", AUTH)[1]
        } >= {"dip-feeds", "dip-dag-runs", "dip-agents"}),
        # Stage 11 (S11.4): the agents' series exist (the queries ran as dip_metrics; CI has
        # no model key, so the chain and job statuses carry them).
        wait_for("the agents' metrics", lambda: {
            r["metric"]["query"] for r in query("dip_metrics_query_ok == 1")
        } >= {"dip_agent_runs_24h", "dip_agent_fallback_runs_24h", "dip_approvals_decided_7d",
              "dip_model_spend_usd_today", "dip_model_calls_24h",
              "dip_model_latency_p95_seconds_24h"}),
    ])  # fmt: skip
    if not ok:
        return 1
    print("PASS: observability profile (Prometheus, alert rules, Grafana dashboards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
