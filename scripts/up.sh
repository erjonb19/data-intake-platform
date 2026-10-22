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
printf '{ "airflow_image_tag": "%s" }\n' "$tag" > "$TF_DIR/image.auto.tfvars.json"

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
