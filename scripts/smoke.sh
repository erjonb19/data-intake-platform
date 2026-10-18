#!/usr/bin/env bash
# Run the end-to-end smoke test against the running stack.
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require terraform

AIRFLOW_URL="$(tf output -raw airflow_url)"
MINIO_URL="$(tf output -raw minio_api_url)"
AIRFLOW_PASSWORD="$(tf output -raw airflow_admin_password)"
export AIRFLOW_URL MINIO_URL AIRFLOW_PASSWORD

exec "$(python_bin)" "$REPO_ROOT/tests/e2e/smoke.py"
