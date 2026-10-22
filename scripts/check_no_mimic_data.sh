#!/usr/bin/env bash
# Fails if git tracks (or, with --staged, is about to commit) anything that must stay local:
#   1. MIMIC-IV files, or any bulk data file (.ndjson, .csv.gz).
#   2. A data file (.csv, .parquet, .duckdb, .json) outside the reviewed paths: test
#      fixtures, dbt seeds and the contract schema.
#   3. Content that only real MIMIC data or a local-only extract would contain: a MIMIC FHIR
#      identifier with a value, a MIMIC FHIR profile URL, or a package marked local-only.
#   4. An OHDSI Athena vocabulary file, or MIT-LCP's lab map (licensed to the downloader).
# Only the pinned checksum files under packs/mimic/ may mention a MIMIC dataset name.
# CI runs it on the checkout; scripts/install-hooks.sh runs it before every commit.
set -euo pipefail

if [[ "${1:-}" == "--staged" ]]; then
  files() { git diff --cached --name-only --diff-filter=ACMR; }
  grep_content() { git grep --cached -I -l -E "$@"; }
else
  files() { git ls-files; }
  grep_content() { git grep -I -l -E "$@"; }
fi

bulk="$(files | grep -E '(^|/)mimic-iv-[^/]*/|\.ndjson(\.gz)?$|\.csv\.gz$' || true)"
data="$(files | grep -E '\.(csv|parquet|duckdb|json)$' \
  | grep -v -E '^tests/fixtures/|^dbt/seeds/[^/]+\.csv$|^dip/contract\.schema\.json$' || true)"
athena="$(files | grep -E '(^|/)(CONCEPT[^/]*|DOMAIN|RELATIONSHIP|VOCABULARY|DRUG_STRENGTH)\.csv$|d_labitems_to_loinc\.csv$' || true)"

# This script and the code that writes the local-only marker are the only allowed hits.
patterns=(
  -e 'mimic\.mit\.edu/fhir/mimic/identifier/[A-Za-z0-9_-]+"[[:space:]]*,[[:space:]]*"value"'
  -e 'mimic\.mit\.edu/fhir/mimic/StructureDefinition/'
  -e '"dip_distribution"[[:space:]]*:[[:space:]]*"local-only"'
)
content="$(grep_content "${patterns[@]}" -- . ':!scripts/check_no_mimic_data.sh' ':!dip/extract.py' || true)"
if [[ -n "$content" ]]; then
  # With --staged, report only files in this commit.
  [[ "${1:-}" == "--staged" ]] && content="$(comm -12 <(echo "$content" | sort) <(files | sort))"
fi

if [[ -n "$bulk$data$athena$content" ]]; then
  echo "ERROR: files that must stay local are tracked by git:" >&2
  [[ -n "$bulk" ]] && printf '  MIMIC or bulk data: %s\n' $bulk >&2
  [[ -n "$athena" ]] && printf '  Athena vocabulary or lab map: %s\n' $athena >&2
  [[ -n "$data" ]] && printf '  data file outside tests/fixtures or dbt/seeds: %s\n' $data >&2
  [[ -n "$content" ]] && printf '  MIMIC identifiers or a local-only package in: %s\n' $content >&2
  exit 1
fi
echo "ok: no MIMIC data, Athena vocabulary, local-only packages or stray data files tracked"
