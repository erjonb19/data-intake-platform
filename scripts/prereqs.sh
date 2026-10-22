#!/usr/bin/env bash
# Checks local prerequisites. Read-only: installs nothing.
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

status=0
check() {
  local name="$1" cmd="$2"
  if command -v "$name" >/dev/null 2>&1; then
    printf '  %-10s %s\n' "$name" "$(eval "$cmd" 2>&1 | head -1)"
  else
    printf '  %-10s MISSING\n' "$name"
    status=1
  fi
}

log "tools"
check docker    "docker --version"
check kind      "kind version"
check terraform "terraform version"
check kubectl   "kubectl version --client"
check helm      "helm version --short"
check "$(python_bin)" "$(python_bin) --version"

log "docker daemon"
if mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null); then
  gib=$((mem / 1024 / 1024 / 1024))
  echo "  memory available to Docker: ${gib} GiB"
  if (( gib < 7 )); then
    warn "less than 7 GiB for Docker; the core profile needs about 4.5 GiB plus headroom"
  fi
else
  echo "  daemon not running"
  status=1
fi

exit "$status"
