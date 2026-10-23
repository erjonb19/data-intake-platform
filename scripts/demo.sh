#!/usr/bin/env bash
# The recorded demo (Stage 8, S8.2; dip/demo.py): one breakage through the whole platform,
# from a quarantined file to an incident, the LangGraph handoff, triage, a refused and then
# an approved Act call, and the vendor's resend, with the audit trail of the chain.
#
# No API key and no model call: the agents' turns are replayed from
# agents/evals/demo/recordings/ through the real intake, MCP server, policies and graph.
# Needs Python with the dev extras (pip install -e ".[dev]") and, unless DIP_EVAL_DSN
# names a throwaway database, Docker: a Postgres container is started for the demo and
# removed afterwards. Every value is invented.
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

py="$(python_bin)"
for venv_py in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe"; do
  [[ -x "$venv_py" ]] && py="$venv_py" && break
done

if [[ -z "${DIP_EVAL_DSN:-}" ]]; then
  require docker
  name="dip-demo-pg"
  port="${DEMO_PG_PORT:-55439}"
  log "starting a throwaway Postgres ($name, port $port)"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --rm --name "$name" -p "127.0.0.1:$port:5432" \
    -e POSTGRES_USER=dip -e POSTGRES_PASSWORD=dip -e POSTGRES_DB=dip_demo_eval \
    postgres:17 >/dev/null
  trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 60); do
    docker exec "$name" pg_isready -U dip -d dip_demo_eval >/dev/null 2>&1 && break
    sleep 1
  done
  export DIP_EVAL_DSN="postgresql://dip:dip@127.0.0.1:$port/dip_demo_eval"
fi

cd "$REPO_ROOT"
PYTHONUTF8=1 "$py" -m dip demo
