#!/usr/bin/env bash
# Collect cluster state and pod logs into ./artifacts (used by CI on failure).
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"
require kubectl

out="$REPO_ROOT/artifacts"
mkdir -p "$out/logs"
export KUBECONFIG="$REPO_ROOT/.kube/kind-$CLUSTER_NAME.yaml"

kubectl get all,pvc,jobs -A -o wide > "$out/resources.txt" 2>&1 || true
kubectl get events -A --sort-by=.lastTimestamp > "$out/events.txt" 2>&1 || true
kubectl describe pods -A > "$out/pods-describe.txt" 2>&1 || true

kubectl get pods -A --no-headers -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name 2>/dev/null |
  while read -r ns pod; do
    kubectl logs -n "$ns" "$pod" --all-containers --prefix > "$out/logs/$ns.$pod.log" 2>&1 || true
  done

log "diagnostics written to $out"
