#!/usr/bin/env bash
# Fails if any MIMIC-IV data (or any compressed bulk data file), or any OHDSI Athena
# vocabulary file, is tracked by git. Only the pinned checksum files under packs/mimic/
# may mention a MIMIC dataset name. Athena files are licensed to whoever downloaded them.
set -euo pipefail
bad="$(git ls-files | grep -E '(^|/)mimic-iv-[^/]*/|\.ndjson(\.gz)?$|\.csv\.gz$' || true)"
athena="$(git ls-files | grep -E '(^|/)(CONCEPT|CONCEPT_RELATIONSHIP|CONCEPT_ANCESTOR|CONCEPT_SYNONYM|DRUG_STRENGTH|VOCABULARY)\.csv$|d_labitems_to_loinc\.csv$' || true)"
if [[ -n "$bad$athena" ]]; then
  echo "ERROR: MIMIC, bulk data or Athena vocabulary files are tracked by git:" >&2
  printf '%s\n' "$bad" "$athena" | sed '/^$/d' >&2
  exit 1
fi
echo "ok: no MIMIC data or Athena vocabulary tracked"
