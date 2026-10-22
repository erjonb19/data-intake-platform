# Shared helpers for scripts/*.sh. Works in Git Bash on Windows and bash on Linux/CI.
# shellcheck shell=bash disable=SC2034

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Native Windows tools (terraform.exe, kind.exe) need C:/... paths, which Git Bash also accepts.
if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT="$(cygpath -m "$REPO_ROOT")"
fi
TF_DIR="$REPO_ROOT/infra/terraform"
CLUSTER_NAME="${CLUSTER_NAME:-dip}"
export MSYS_NO_PATHCONV=1

# winget installs land outside Git Bash's PATH until a new login session.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  winget_pkgs="${LOCALAPPDATA:-}/Microsoft/WinGet/Packages"
  if [[ -d "$winget_pkgs" ]]; then
    for d in "$winget_pkgs"/Kubernetes.kind_* "$winget_pkgs"/Hashicorp.Terraform_* \
             "$winget_pkgs"/Helm.Helm_*/windows-amd64; do
      [[ -d "$d" ]] && PATH="$PATH:$(cygpath -u "$d")"
    done
  fi
  export PATH
fi

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found on PATH (run scripts/prereqs.sh)"
  done
}

tf() { terraform -chdir="$TF_DIR" "$@"; }

python_bin() {
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    echo python3
  else
    echo python
  fi
}
