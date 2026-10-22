#!/usr/bin/env bash
# Download the public MIMIC-IV demo datasets from PhysioNet into data/mimic/ (gitignored).
#
#   MIMIC-IV Clinical Database Demo on FHIR v2.1.0  -> data/mimic/mimic-iv-fhir-demo-2.1.0/
#   MIMIC-IV Clinical Database Demo v2.2            -> data/mimic/mimic-iv-demo-2.2/
#
# Both are open access under the Open Data Commons Open Database License v1.0 (ODbL).
# They are REAL, de-identified patient records, not synthetic. Never commit them or anything
# derived from them; see PLAN.md section 8.2 and docs/research/mimic.md (citations).
#
# Every file is checked against the SHA256SUMS pinned in this folder, whichever server it
# came from. Re-running skips files that are already present and correct.
#
# Usage: packs/mimic/fetch.sh [fhir|relational|all]   (default: all)
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/../../scripts/lib.sh"
require curl sha256sum unzip

WHICH="${1:-all}"
DEST_ROOT="${MIMIC_DIR:-$REPO_ROOT/data/mimic}"
# Native Windows curl.exe needs C:/... paths, as in lib.sh.
command -v cygpath >/dev/null 2>&1 && DEST_ROOT="$(cygpath -m "$DEST_ROOT")"
mkdir -p "$DEST_ROOT"

# One run at a time: two runs writing the same partial file corrupt it.
LOCK="$DEST_ROOT/.fetch.lock"
mkdir "$LOCK" 2>/dev/null || die "another fetch is running (remove $LOCK if it is not)"
trap 'rm -rf "$LOCK"' EXIT

# The file's pinned digest, or empty when the file is missing or wrong.
verified() { [[ -f "$1" ]] && [[ "$(sha256sum "$1" | cut -d' ' -f1)" == "$2" ]]; }

# PhysioNet's open-data S3 mirror serves some projects as one ZIP, much faster than
# PhysioNet itself. Unpack whatever verifies; anything else falls back to PhysioNet.
from_mirror_zip() {
  local name="$1" zip_url="$2" inner="$3" sums="$REPO_ROOT/packs/mimic/$1.sha256"
  local dest="$DEST_ROOT/$name" tmp="$DEST_ROOT/.zip-$name"
  rm -rf "$tmp" && mkdir -p "$tmp"
  log "trying the PhysioNet S3 mirror for $name"
  if ! curl -fsSL -o "$tmp/archive.zip" "$zip_url"; then
    warn "mirror unavailable; using PhysioNet directly"
    rm -rf "$tmp"
    return 0
  fi
  unzip -q -o "$tmp/archive.zip" -d "$tmp/x"
  while read -r digest path; do
    [[ -z "$digest" ]] && continue
    if verified "$tmp/x/$inner/$path" "$digest"; then
      mkdir -p "$(dirname "$dest/$path")"
      mv "$tmp/x/$inner/$path" "$dest/$path"
    fi
  done < "$sums"
  rm -rf "$tmp"
}

fetch() {
  local name="$1" base_url="$2" sums="$REPO_ROOT/packs/mimic/$1.sha256"
  local dest="$DEST_ROOT/$name" n=0 attempt
  mkdir -p "$dest"
  log "fetching $name into $dest"
  while read -r digest path; do
    [[ -z "$digest" ]] && continue
    local file="$dest/$path"
    verified "$file" "$digest" && continue
    mkdir -p "$(dirname "$file")"
    # PhysioNet resets long transfers now and then: resume the partial file (-C -). No
    # curl-internal --retry: it restarts from zero instead of resuming.
    for attempt in 1 2 3 4 5 6; do
      curl -fsSL -C - -o "$file.part" "$base_url/$path" && break
      warn "download of $path interrupted (attempt $attempt); resuming"
      sleep $((attempt * 5))
    done
    verified "$file.part" "$digest" || die "checksum mismatch for $name/$path"
    mv "$file.part" "$file"
    n=$((n + 1))
  done < "$sums"
  log "$name: $n file(s) from PhysioNet, all $(wc -l < "$sums" | tr -d ' ') verified"
}

fhir() {
  local name=mimic-iv-fhir-demo-2.1.0
  if [[ ! -f "$DEST_ROOT/$name/fhir/MimicPatient.ndjson.gz" ]]; then
    from_mirror_zip "$name" \
      https://physionet-open.s3.amazonaws.com/mimic-iv-fhir-demo/mimic-iv-fhir-demo-2.1.0.zip \
      mimic-iv-clinical-database-demo-on-fhir-2.1.0
  fi
  fetch "$name" https://physionet.org/files/mimic-iv-fhir-demo/2.1.0
}

relational() {
  fetch mimic-iv-demo-2.2 https://physionet.org/files/mimic-iv-demo/2.2
}

case "$WHICH" in
  fhir) fhir ;;
  relational) relational ;;
  all) fhir && relational ;;
  *) die "usage: $0 [fhir|relational|all]" ;;
esac
