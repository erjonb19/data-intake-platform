#!/usr/bin/env bash
# What an in-place upgrade must keep: the Postgres cluster's identity (the same data volume)
# and the row counts of the tables the e2e fills. Used by .github/workflows/upgrade.yml,
# which copies this script aside so the same version runs before and after the upgrade.
#
#   KUBECONFIG=... bash scripts/upgrade_snapshot.sh > before.txt
#   ...upgrade, rerun the e2e...
#   KUBECONFIG=... bash scripts/upgrade_snapshot.sh > after.txt
#   bash scripts/upgrade_snapshot.sh --compare before.txt after.txt
#
# Self-contained (no lib.sh): the checkout around it changes version mid-run.
set -euo pipefail

TABLES=(
  ops.intake_file
  ops.quarantine
  ops.dq_results
  gold.member_eligibility
  omop.person
  omop.condition_occurrence
  omop.measurement
  omop_shareable.person
)

if [[ "${1:-}" == "--compare" ]]; then
  before="$2" after="$3"
  # Same cluster identity. Intake records (ops.*) never lose rows: a rerun may add, never
  # remove. dbt-built tables are rebuilt from bronze, so a new version may change their
  # counts on purpose: shrinking is a warning, but emptying or losing one is a failure.
  # The system identifier is 64 bits: compared as a string (awk numbers are doubles).
  awk '
    NR == FNR { was[$1] = $2; next }
    { now[$1] = $2 }
    END {
      bad = 0
      for (k in was) {
        if (!(k in now)) { printf "MISSING %s\n", k; bad = 1; continue }
        if (k == "system_identifier") {
          if ((now[k] "") != (was[k] "")) { printf "FAIL Postgres was re-initialized (%s -> %s)\n", was[k], now[k]; bad = 1 }
          else printf "ok   same Postgres data volume (system identifier %s)\n", now[k]
        } else if (was[k] > 0 && now[k] <= 0) {
          printf "FAIL %s emptied or gone: %s -> %s\n", k, was[k], now[k]; bad = 1
        } else if (was[k] >= 0 && now[k] < was[k] && k ~ /^ops\./) {
          printf "FAIL %s shrank: %s -> %s\n", k, was[k], now[k]; bad = 1
        } else if (was[k] >= 0 && now[k] < was[k]) {
          printf "WARN %s shrank (dbt rebuild): %s -> %s\n", k, was[k], now[k]
        } else {
          printf "ok   %s %s -> %s\n", k, was[k], now[k]
        }
      }
      exit bad
    }' "$before" "$after"
  exit
fi

ns="${DIP_NAMESPACE:-dip-data}"
sql() {
  kubectl -n "$ns" exec postgres-0 -- psql -U postgres -d warehouse -AtX -v ON_ERROR_STOP=1 -c "$1"
}

echo "system_identifier $(sql 'select system_identifier from pg_control_system()')"
for t in "${TABLES[@]}"; do
  if [[ "$(sql "select to_regclass('$t') is not null")" == "t" ]]; then
    echo "$t $(sql "select count(*) from $t")"
  else
    echo "$t -1" # not there in this version
  fi
done
