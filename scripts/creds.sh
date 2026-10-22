#!/usr/bin/env bash
# Print local credentials for the stack (local, generated, never committed).
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require terraform

echo "airflow  admin / $(tf output -raw airflow_admin_password)"
echo "minio    $(tf output -raw minio_root_user) / $(tf output -raw minio_root_password)"
echo "postgres $(tf output -raw warehouse_dsn)"
