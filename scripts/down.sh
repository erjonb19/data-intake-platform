#!/usr/bin/env bash
# Delete the local stack. All state lives inside the kind cluster, so deleting the
# cluster and its Terraform state is a complete teardown.
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require kind

log "deleting kind cluster '$CLUSTER_NAME'"
kind delete cluster --name "$CLUSTER_NAME"

log "removing local Terraform state and kubeconfig"
rm -f "$TF_DIR"/terraform.tfstate "$TF_DIR"/terraform.tfstate.backup \
      "$TF_DIR"/image.auto.tfvars.json "$REPO_ROOT/.kube/kind-$CLUSTER_NAME.yaml"
