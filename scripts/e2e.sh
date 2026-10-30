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
COHORT_READER_DSN="$(tf output -raw cohort_reader_dsn)"
export AIRFLOW_URL MINIO_URL AIRFLOW_PASSWORD MINIO_USER MINIO_PASSWORD WAREHOUSE_DSN COHORT_READER_DSN

py="$(python_bin)"
for venv_py in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe"; do
  [[ -x "$venv_py" ]] && py="$venv_py" && break
done

# Stage 2: the SFTP channel. The e2e reaches the server through a port-forward (no new
# host port, so an existing cluster needs no rebuild) with each vendor's own key.
SFTP_PORT="${SFTP_PORT:-2222}"
SFTP_KEY_DIR="$(mktemp -d)"
# Git Bash on Windows: a /tmp path the native Python can open too.
command -v cygpath >/dev/null 2>&1 && SFTP_KEY_DIR="$(cygpath -m "$SFTP_KEY_DIR")"
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

# Stage 2: the intake API, also through a port-forward, with each vendor's API key.
API_PORT="${API_PORT:-8001}"
INTAKE_API_URL="http://127.0.0.1:$API_PORT"
INTAKE_API_KEYS="$(tf output -json intake_api_keys)"
export INTAKE_API_URL INTAKE_API_KEYS
kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-data \
  port-forward svc/intake-api "$API_PORT:8000" >/dev/null 2>&1 &
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
wait_port "$API_PORT"

"$py" "$REPO_ROOT/tests/e2e/fhir_intake.py"
# Stage 1+: the table-driven file-feed cases (tests/e2e/cases.yaml).
"$py" "$REPO_ROOT/tests/e2e/run.py"

# Stage 3: the observability profile, when deployed (CI's E2E deploys it).
if tf output -json profiles | grep -q '"observability"'; then
  PROM_PORT="${PROM_PORT:-9090}"
  GRAFANA_PORT="${GRAFANA_PORT:-3000}"
  kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-obs \
    port-forward svc/prometheus-server "$PROM_PORT:80" >/dev/null 2>&1 &
  pids+=("$!")
  kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-obs \
    port-forward svc/grafana "$GRAFANA_PORT:80" >/dev/null 2>&1 &
  pids+=("$!")
  wait_port "$PROM_PORT"
  wait_port "$GRAFANA_PORT"
  PROMETHEUS_URL="http://127.0.0.1:$PROM_PORT" GRAFANA_URL="http://127.0.0.1:$GRAFANA_PORT" \
    GRAFANA_PASSWORD="$(tf output -raw grafana_admin_password)" \
    "$py" "$REPO_ROOT/tests/e2e/observability.py"
  # S3.3: lineage from a landed file to gold, in Marquez.
  MARQUEZ_PORT="${MARQUEZ_PORT:-5000}"
  kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-obs \
    port-forward svc/marquez "$MARQUEZ_PORT:5000" >/dev/null 2>&1 &
  pids+=("$!")
  wait_port "$MARQUEZ_PORT"
  MARQUEZ_URL="http://127.0.0.1:$MARQUEZ_PORT" "$py" "$REPO_ROOT/tests/e2e/lineage.py"
fi

# Stage 3 (S3.4): the deployed MCP Read tier, after everything above has run (its tools
# read the quarantine, runs, metrics and lineage those left behind).
MCP_PORT="${MCP_PORT:-8765}"
kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-data \
  port-forward svc/mcp "$MCP_PORT:8765" >/dev/null 2>&1 &
pids+=("$!")
wait_port "$MCP_PORT"
OBSERVABILITY=0
tf output -json profiles | grep -q '"observability"' && OBSERVABILITY=1
MCP_URL="http://127.0.0.1:$MCP_PORT" MCP_READER_KEY="$(tf output -raw mcp_reader_key)" \
  OBSERVABILITY="$OBSERVABILITY" "$py" "$REPO_ROOT/tests/e2e/mcp_read.py"

# Stage 4 (S4.5): one break-and-fix loop through the deployed stack: it closes the
# eligibility feed's missing-column group (the corrected file loads, the entries are
# superseded). Every scenario's model-driven loop runs on recordings in CI's test job.
MCP_URL="http://127.0.0.1:$MCP_PORT" MCP_READER_KEY="$(tf output -raw mcp_reader_key)" \
  MCP_TRIAGE_KEY="$(tf output -raw mcp_triage_key)" "$py" "$REPO_ROOT/tests/e2e/break_loop.py"

# Stage 9 (S9.2): the approvals page, after the break loop left an executed approval and
# a denied Act call behind. It runs as its own login (dip_approver).
APPROVALS_PORT="${APPROVALS_PORT:-8081}"
kubectl --kubeconfig "$(tf output -raw kubeconfig_path)" -n dip-data \
  port-forward svc/approvals "$APPROVALS_PORT:8081" >/dev/null 2>&1 &
pids+=("$!")
wait_port "$APPROVALS_PORT"
APPROVALS_URL="http://127.0.0.1:$APPROVALS_PORT" APPROVALS_TOKEN="$(tf output -raw approvals_token)"   "$py" "$REPO_ROOT/tests/e2e/approvals_page.py"
