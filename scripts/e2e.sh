#!/usr/bin/env bash
# Run the end-to-end tests against the running stack (after smoke.sh): the research-track
# FHIR flow, then the file-feed cases in tests/e2e/cases.yaml.
# Needs boto3, psycopg and pyyaml: uses .venv if present (pip install -e ".[dev]").
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require terraform kubectl

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

# Stage 2: the SFTP channel. The e2e reaches the server through a port-forward (no new
# host port, so an existing cluster needs no rebuild) with each vendor's own key.
SFTP_PORT="${SFTP_PORT:-2222}"
SFTP_KEY_DIR="$(mktemp -d)"
pids=()
# Set before anything can fail: the vendor keys and port-forwards never outlive the run.
cleanup() { kill "${pids[@]}" 2>/dev/null || true; rm -rf "$SFTP_KEY_DIR"; }
trap cleanup EXIT
SFTP_HOST_KEY="$(tf output -raw sftp_host_key)"
export SFTP_PORT SFTP_KEY_DIR SFTP_HOST_KEY
tf output -json sftp_vendor_keys | "$py" -c '
import json, os, sys
for account, key in json.load(sys.stdin).items():
    path = os.path.join(os.environ["SFTP_KEY_DIR"], account + ".key")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)  # never world-readable
    with os.fdopen(fd, "wb") as f:
        f.write(key.encode())
'
kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-data \
  port-forward svc/sftp "$SFTP_PORT:22" >/dev/null 2>&1 &
pids+=("$!")
wait_port() {
  local _
  for _ in $(seq 1 30); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    sleep 1
  done
  die "port-forward to 127.0.0.1:$1 did not come up"
}
wait_port "$SFTP_PORT"

"$py" "$REPO_ROOT/tests/e2e/fhir_intake.py"
# Stage 1+: the table-driven file-feed cases (tests/e2e/cases.yaml).
"$py" "$REPO_ROOT/tests/e2e/run.py"
