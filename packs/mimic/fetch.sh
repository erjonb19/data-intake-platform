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
# Every file is checked against the SHA256SUMS pinned in this folder. Re-running skips
# files that are already present and correct.
#
# Usage: packs/mimic/fetch.sh [fhir|relational|all]   (default: all)
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/../../scripts/lib.sh"
require curl sha256sum

WHICH="${1:-all}"
DEST_ROOT="${MIMIC_DIR:-$REPO_ROOT/data/mimic}"

fetch() {
  local name="$1" base_url="$2" sums="$REPO_ROOT/packs/mimic/$1.sha256"
  local dest="$DEST_ROOT/$name" n=0
  mkdir -p "$dest"
  log "fetching $name into $dest"
  while read -r digest path; do
    [[ -z "$digest" ]] && continue
    local file="$dest/$path"
    if [[ -f "$file" ]] && [[ "$(sha256sum "$file" | cut -d' ' -f1)" == "$digest" ]]; then
      continue
    fi
    mkdir -p "$(dirname "$file")"
    # PhysioNet resets long transfers now and then: resume the partial file and retry.
    local attempt
    for attempt in 1 2 3 4 5 6; do
      curl -fsSL --retry 3 --retry-all-errors -C - -o "$file.part" "$base_url/$path" && break
      warn "download of $path interrupted (attempt $attempt); resuming"
      sleep $((attempt * 5))
    done
    [[ "$(sha256sum "$file.part" | cut -d' ' -f1)" == "$digest" ]] \
      || die "checksum mismatch for $name/$path"
    mv "$file.part" "$file"
    n=$((n + 1))
  done < "$sums"
  log "$name: $n file(s) downloaded, all $(wc -l < "$sums" | tr -d ' ') verified"
}

case "$WHICH" in
  fhir | all) fetch mimic-iv-fhir-demo-2.1.0 https://physionet.org/files/mimic-iv-fhir-demo/2.1.0 ;;&
  relational | all) fetch mimic-iv-demo-2.2 https://physionet.org/files/mimic-iv-demo/2.2 ;;
  fhir) ;;
  *) die "usage: $0 [fhir|relational|all]" ;;
esac
