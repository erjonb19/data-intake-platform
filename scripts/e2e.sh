#!/usr/bin/env bash
# Run the end-to-end tests against the running stack (after smoke.sh): the research-track
# FHIR flow, then the file-feed cases in tests/e2e/cases.yaml.
# Needs boto3, psycopg and pyyaml: uses .venv if present (pip install -e ".[dev]").
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require terraform

AIRFLOW_URL="$(tf output -raw airflow_url)"
MINIO_URL="$(tf output -raw minio_api_url)"
AIRFLOW_PASSWORD="$(tf output -raw airflow_admin_password)"
MINIO_USER="$(tf output -raw minio_root_user)"
MINIO_PASSWORD="$(tf output -raw minio_root_password)"
WAREHOUSE_DSN="$(tf output -raw warehouse_dsn)"
export AIRFLOW_URL MINIO_URL AIRFLOW_PASSWORD MINIO_USER MINIO_PASSWORD WAREHOUSE_DSN

py="$(python_bin)"
for venv_py in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe"; do
  [[ -x "$venv_py" ]] && py="$venv_py" && break
done

"$py" "$REPO_ROOT/tests/e2e/fhir_intake.py"
# Stage 1+: the table-driven file-feed cases (tests/e2e/cases.yaml).
"$py" "$REPO_ROOT/tests/e2e/run.py"
