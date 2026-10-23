#!/usr/bin/env bash
# Create (or update) the local stack: kind cluster, platform image, then everything else.
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require docker kind terraform

log "terraform init"
tf init -input=false -upgrade=false >/dev/null

# Phase 1: the cluster on its own, so the image can be loaded before Helm deploys Airflow.
log "creating kind cluster '$CLUSTER_NAME'"
tf apply -input=false -auto-approve -target=kind_cluster.this

log "building platform Airflow image"
docker build -t dip-airflow:build -f "$REPO_ROOT/images/airflow/Dockerfile" "$REPO_ROOT"
# Content-addressed tag, so a rebuilt image always rolls the Airflow pods.
tag="$(docker image inspect -f '{{.Id}}' dip-airflow:build | sed 's/^sha256://' | cut -c1-12)"
docker tag dip-airflow:build "dip-airflow:$tag"

log "loading dip-airflow:$tag into kind"
kind load docker-image "dip-airflow:$tag" --name "$CLUSTER_NAME"

log "building the OCR task image (Airflow + Tesseract)"
docker build -t dip-ocr:build --build-arg "BASE=dip-airflow:$tag" \
  -f "$REPO_ROOT/images/ocr/Dockerfile" "$REPO_ROOT"
ocr_tag="$(docker image inspect -f '{{.Id}}' dip-ocr:build | sed 's/^sha256://' | cut -c1-12)"
docker tag dip-ocr:build "dip-ocr:$ocr_tag"
kind load docker-image "dip-ocr:$ocr_tag" --name "$CLUSTER_NAME"

log "building the MCP server image (the Read tier)"
docker build -t dip-mcp:build -f "$REPO_ROOT/images/mcp/Dockerfile" "$REPO_ROOT"
mcp_tag="$(docker image inspect -f '{{.Id}}' dip-mcp:build | sed 's/^sha256://' | cut -c1-12)"
docker tag dip-mcp:build "dip-mcp:$mcp_tag"
kind load docker-image "dip-mcp:$mcp_tag" --name "$CLUSTER_NAME"

log "building the intake API image"
docker build -t dip-intake-api:build -f "$REPO_ROOT/images/intake-api/Dockerfile" "$REPO_ROOT"
api_tag="$(docker image inspect -f '{{.Id}}' dip-intake-api:build | sed 's/^sha256://' | cut -c1-12)"
docker tag dip-intake-api:build "dip-intake-api:$api_tag"
kind load docker-image "dip-intake-api:$api_tag" --name "$CLUSTER_NAME"

printf '{ "airflow_image_tag": "%s", "intake_api_image_tag": "%s", "ocr_image_tag": "%s", "mcp_image_tag": "%s" }\n' \
  "$tag" "$api_tag" "$ocr_tag" "$mcp_tag" > "$TF_DIR/image.auto.tfvars.json"

# The chart's migration and create-user Jobs are plain Jobs (no Helm hooks, so Terraform's
# wait works), and a Job's pod template is immutable: a Helm upgrade that changes them (a
# new image tag) fails with "field is immutable". On an existing install, delete them
# first; Helm recreates them, so migrations rerun on the new image.
kubeconfig="$REPO_ROOT/.kube/kind-$CLUSTER_NAME.yaml"
if [[ -f "$kubeconfig" ]] && command -v kubectl >/dev/null 2>&1; then
  kubectl --kubeconfig "$kubeconfig" -n dip-airflow delete job --ignore-not-found \
    airflow-run-airflow-migrations airflow-create-user ||
    warn "could not delete the chart's Jobs; an upgrade may fail with 'field is immutable'"
fi

# Phase 2: everything else.
log "deploying the stack"
tf apply -input=false -auto-approve

log "stack is up"
cat <<EOF
  Airflow UI     $(tf output -raw airflow_url)   (user: admin, password: bash scripts/creds.sh)
  MinIO API      $(tf output -raw minio_api_url)
  MinIO console  $(tf output -raw minio_console_url)
  kubeconfig     export KUBECONFIG="$(tf output -raw kubeconfig_path)"
EOF
