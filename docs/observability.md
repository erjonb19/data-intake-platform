# Observability: Prometheus, alert rules and Grafana (Stage 3, S3.2)

An opt-in profile, `profiles = ["core", "observability"]`. CI's E2E always deploys it. On
the laptop, turn it on only when working on monitoring, because it needs about 0.6 GB more
(PLAN.md section 5):

```bash
TF_VAR_profiles='["core", "observability"]' bash scripts/up.sh
kubectl -n dip-obs port-forward svc/prometheus-server 9090:80   # http://localhost:9090
kubectl -n dip-obs port-forward svc/grafana 3000:80             # http://localhost:3000, user admin
terraform -chdir=infra/terraform output -raw grafana_admin_password
```

## What runs

| Piece | Where | What it does |
|---|---|---|
| Prometheus (the plain `prometheus-community/prometheus` chart, not kube-prometheus-stack) | `dip-obs` | Scrapes every 30 s, keeps 1 day, and evaluates `observability/prometheus/rules.yml`. There is no Alertmanager, node-exporter, kube-state-metrics, pushgateway, or cluster-wide RBAC |
| statsd-exporter (the Airflow chart's own) | `dip-airflow` | Turns Airflow's StatsD metrics into Prometheus metrics (`airflow_*`). Enabled only with this profile |
| Warehouse exporter (`python -m dip metrics-serve`, `dip/metrics.py`) | `dip-data` | Feed metrics from the ops tables (`dip_*`). Runs in the platform image as the `dip_metrics` login |
| Grafana (`grafana-community/grafana`) | `dip-obs` | Two provisioned, read-only dashboards: Feeds, and DAG runs. No anonymous access and no sign-up |

## The warehouse exporter's access

`dip_metrics` is a login with column grants on the ops columns its queries name, and
nothing else:

- `ops.intake_file`: feed, status, row counts, received_at
- `ops.quarantine`: feed, reason_code, status
- `ops.dq_results`: test name, category, status, failures, time
- `ops.incident`: feed, kind, closed_at

It cannot read bronze, silver, gold, the research tiers, a quarantine row's detail or record
reference, file names, or the audit log. It is read-only, with 10 s statements and at most
3 connections (`infra/terraform/files/postgres-roles.sql`).

The grants are made where each table is created, by `ops.grant_metrics_reader()` in
`dip/sql/warehouse.sql`, which `dip/sla.py` and the dbt `store_dq_results` hook also call. So
a table created later is readable as soon as it exists. A query that cannot run yet shows
as `dip_metrics_query_ok{query=...} 0`, and the other queries still report.
`tests/integration/test_metrics.py` connects as the role and checks both what it reads and
what it is refused.

## Metrics and alerts

| Metric | Meaning |
|---|---|
| `dip_intake_files{feed,status}` / `dip_files_24h` | Files, all time and in the last 24 h, by outcome |
| `dip_rows_24h{feed,outcome}` | Rows accepted and rejected, last 24 h |
| `dip_feed_last_received_timestamp_seconds{feed}` | Freshness: `time() -` this |
| `dip_quarantine_open{feed,reason_code}` | Quarantine rows waiting for a person |
| `dip_dq_test_status{test_name,dq_category,status}`, `dip_dq_test_failures` | Each dbt test's latest result |
| `dip_incidents_open{feed,kind}` | Open incidents (`FEED.LATE`) |
| `airflow_dagrun_failed_count{dag_id}`, `airflow_dagrun_duration{dag_id}`, ... | Airflow, via StatsD |

Alerts (`observability/prometheus/rules.yml`) appear on Prometheus's `/alerts` page:

- **FeedLate**: an open `FEED.LATE` incident
- **QuarantineRateHigh**: over 20% of a feed's files quarantined in 24 h
- **RowRejectionRateHigh**: over 10% of rows rejected
- **DataQualityError**: a dbt test's latest result is an error. A warning is a finding, not an alert
- **DagRunFailing**: a failed run in the last hour
- **MetricsQueryFailing**, **TargetDown**

The thresholds suit the demo feeds. The E2E quarantines files on purpose, so after a run
QuarantineRateHigh fires for those feeds. Nothing is sent anywhere, since there is no
Alertmanager.

## Tests

- **CI lint:** `promtool check rules` and `promtool test rules` run on `rules_test.yml`,
  which checks each alert's labels and text on given series. The dashboards must also match
  their generator, `observability/grafana/make_dashboards.py`; edit that, not the JSON.
- **CI E2E:** `tests/e2e/observability.py` runs after the file-feed cases. It checks that all
  three targets are up, that every exporter query runs as `dip_metrics`, that the feeds the
  E2E ran have metrics, that Airflow's StatsD metrics are present, and that the 7 rules are
  loaded. It also checks that Grafana is up, refuses an anonymous API call, and has both
  dashboards.
