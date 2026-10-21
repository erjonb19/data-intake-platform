#!/usr/bin/env bash
# Fails if any MIMIC-IV data (or any compressed bulk data file) is tracked by git.
# Only the pinned checksum files under packs/mimic/ may mention a MIMIC dataset name.
set -euo pipefail
bad="$(git ls-files | grep -E '(^|/)mimic-iv-[^/]*/|\.ndjson(\.gz)?$|\.csv\.gz$' || true)"
if [[ -n "$bad" ]]; then
  echo "ERROR: MIMIC or bulk data files are tracked by git:" >&2
  echo "$bad" >&2
  exit 1
fi
echo "ok: no MIMIC data tracked"
