# Data Intake Platform

A free, enterprise-style platform for vendor and partner files that arrive late, change
format, or contain bad records. Files are checked against YAML data contracts, then
either flow through bronze, silver, and gold layers (dbt on Postgres) or land in
quarantine with a machine-readable reason. Lineage, data quality, and metrics are
built in, and AI agents (later stages) can propose fixes, but a human always approves them.

See [PLAN.md](PLAN.md) for the architecture, decisions, and staged roadmap.

**Status: Stage 0 (foundations) complete.**

## Quick start

Prerequisites: Docker (with about 8 GiB of memory available), kind, Terraform and kubectl.
Helm is optional, for debugging only. Check with:

```bash
bash scripts/prereqs.sh
```

Bring the stack up, run the smoke test, and tear it down. On Windows, run these from
Git Bash; `make up` and the other targets work where `make` is installed.

```bash
bash scripts/up.sh       # kind cluster + Postgres + MinIO + Airflow (about 5 minutes on first run)
bash scripts/smoke.sh    # end-to-end smoke test
bash scripts/creds.sh    # local credentials
bash scripts/down.sh     # delete everything
```

| Service | URL |
|---|---|
| Airflow UI | http://localhost:8080 (user `admin`) |
| MinIO console | http://localhost:9001 |
| MinIO S3 API | http://localhost:9000 |
| Postgres (warehouse) | `localhost:15432` |

All ports are bound to `127.0.0.1` only.

## What runs where

| Namespace | Component | Notes |
|---|---|---|
| `dip-airflow` | Airflow 3.2.2 (official Helm chart 1.22.0) | KubernetesExecutor: every task is its own pod; task logs go to MinIO |
| `dip-data` | Postgres 17 | Shared instance with `airflow`, `warehouse` (bronze, silver, gold, ops schemas) and `marquez` databases |
| `dip-data` | MinIO | Buckets: `landing`, `raw`, `quarantine`, `airflow-logs` |

## Repo layout

```
infra/terraform/     kind cluster, namespaces, secrets, Postgres, MinIO, Airflow release
infra/helm-values/   values for the official Airflow chart
images/airflow/      platform Airflow image (DAGs baked in)
dags/                Airflow DAGs
tests/e2e/           end-to-end tests run locally and in CI
scripts/             up, down, smoke, creds, diagnostics, prereqs, model check
config/              LLM configuration (agents arrive in Stage 5)
docs/demos/          per-stage demo scripts
```

## CI

- `ci` workflow: ruff, shellcheck, and Terraform fmt and validate.
- `e2e` workflow: creates a kind cluster with Terraform on a GitHub runner, deploys the
  stack, and runs the end-to-end tests on every push and pull request.

All data in this project is synthetic.
