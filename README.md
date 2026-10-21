# Data Intake Platform

A free, enterprise-style platform for vendor and partner files that arrive late, change
format, or contain bad records. Files are checked against YAML data contracts, then
either flow through bronze, silver, and gold layers (dbt on Postgres) or land in
quarantine with a machine-readable reason. Lineage, data quality, and metrics are
built in, and AI agents (later stages) can propose fixes, but a human always approves them.

See [PLAN.md](PLAN.md) for the architecture, decisions, and staged roadmap.

**Status: Stage 0 (foundations) complete. Research-data track R1–R6 and M1–M3 built** (see
[docs/research](docs/research/README.md)):

| | |
|---|---|
| R1 | Synthea FHIR R4 through the intake: data contract, sha256 raw copy, quarantine with reason codes |
| R2 | dbt gold layer as OMOP CDM 5.4 (person, observation_period, visit, condition, measurement, drug_exposure, observation) |
| R3 | OMOP data quality: dbt tests named after OHDSI DQD checks, results in `ops.dq_results` |
| R4 | De-identified shareable tier (keyed pseudonyms, date shifting, top-coding) with Postgres-enforced access |
| R5 | Versioned Frictionless extracts, proven byte-identical across independent rebuilds |
| R6 | OpenLineage events from intake, dbt and extracts (Marquez arrives in Stage 3) |
| M1 | MIMIC-IV FHIR demo (Bulk Data) through the FHIR intake into OMOP, with a report of what breaks against Synthea |
| M2 | MIMIC-IV relational demo through a relational-tables-to-OMOP path (per-table contracts) |
| M3 | Reconciliation of the two MIMIC OMOP builds by source keys: 0 unexplained differences |

**Data.** Everything committed, and everything CI runs on, is synthetic (Synthea and
hand-written fixtures). Separately, two public **real, de-identified** datasets, the
MIMIC-IV demos from PhysioNet (ODbL), can be downloaded and run **locally only**. They are
never committed, and only aggregate results are published. See
[docs/research/mimic.md](docs/research/mimic.md).

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

- `ci` workflow: ruff, shellcheck, Terraform fmt and validate, unit tests, Postgres
  integration tests, and a fresh Synthea export pushed through the intake.
- `e2e` workflow: creates a kind cluster with Terraform on a GitHub runner, deploys the
  stack, and runs the end-to-end tests on every push and pull request.

CI uses synthetic data only (Synthea and hand-written fixtures). The MIMIC-IV demos are
never downloaded in CI.

## Data sources and citations

| Source | Kind | License | Where it runs |
|---|---|---|---|
| [Synthea](https://github.com/synthetichealth/synthea) v4.0.0 | synthetic | Apache-2.0 (tool); extracts CC0-1.0 | CI and local |
| [OHDSI Eunomia](https://github.com/OHDSI/EunomiaDatasets) Synthea27Nj vocabulary shard | OMOP vocabulary subset | Apache-2.0 | CI and local |
| [MIMIC-IV Clinical Database Demo on FHIR](https://physionet.org/content/mimic-iv-fhir-demo/2.1.0/) v2.1.0 | **real, de-identified** | ODbL v1.0 | local only |
| [MIMIC-IV Clinical Database Demo](https://physionet.org/content/mimic-iv-demo/2.2/) v2.2 | **real, de-identified** | ODbL v1.0 | local only |

When using the MIMIC-IV demos, cite:

- Bennett, A., Ulrich, H., Wiedekopf, J., Szul, P., Grimes, J., & Johnson, A. (2025).
  MIMIC-IV Clinical Database Demo on FHIR (version 2.1.0). PhysioNet. RRID:SCR_007345.
  https://doi.org/10.13026/vphg-y548
- Bennett AM, Ulrich H, van Damme P, Wiedekopf J, Johnson AE. MIMIC-IV on FHIR: converting a
  decade of in-patient data into an exchangeable, interoperable format. JAMIA.
  2023;30(4):718-25. https://doi.org/10.1093/jamia/ocad002
- Johnson, A., Bulgarelli, L., Pollard, T., Horng, S., Celi, L. A., & Mark, R. (2023).
  MIMIC-IV Clinical Database Demo (version 2.2). PhysioNet. RRID:SCR_007345.
  https://doi.org/10.13026/dp1f-ex47
- Johnson, A., Bulgarelli, L., Pollard, T., Celi, L. A., Horng, S., & Mark, R. (2023).
  MIMIC-IV-ED Demo (version 2.2). PhysioNet. RRID:SCR_007345.
  https://doi.org/10.13026/jzz5-vs76
- Pollard, T., Moody, B. E., Lehman, L., Gow, B., Fernandes, C., Xie, C., Johnson, A.,
  Mark, R. G., & Heldt, T. (2026). PhysioNet as a global platform for biomedical research.
  Nature Health. https://doi.org/10.1038/s44360-026-00096-z
