#!/usr/bin/env bash
# Generate synthetic patients with Synthea as FHIR R4 bundles, one file per patient.
#
# Runs the pinned Synthea release inside a Java container, so nothing is installed on the
# host. Same release + seed + reference date gives the same patients every time.
#
# Usage: packs/healthcare/generators/synthea.sh [population] [out_dir]
# Env:   SYNTHEA_SEED (default 42), SYNTHEA_REFERENCE_DATE (default 20260101),
#        SYNTHEA_END_DATE (default: the reference date), SYNTHEA_STATE
#
# All output is synthetic. Synthea: https://github.com/synthetichealth/synthea (Apache-2.0).
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/../../../scripts/lib.sh"
require docker

POPULATION="${1:-50}"
OUT_DIR="${2:-$REPO_ROOT/data/synthea}"
SEED="${SYNTHEA_SEED:-42}"
REFERENCE_DATE="${SYNTHEA_REFERENCE_DATE:-20260101}"
# Without a fixed end date Synthea simulates up to *today*, so output changes day to day.
END_DATE="${SYNTHEA_END_DATE:-$REFERENCE_DATE}"
STATE="${SYNTHEA_STATE:-New York}"

SYNTHEA_VERSION="v4.0.0"
SYNTHEA_SHA256="${SYNTHEA_SHA256:-}"
JAVA_IMAGE="eclipse-temurin:21-jre"
CACHE_DIR="${SYNTHEA_CACHE:-$REPO_ROOT/data/cache}"
JAR="$CACHE_DIR/synthea-${SYNTHEA_VERSION}.jar"

mkdir -p "$CACHE_DIR" "$OUT_DIR"
if [[ ! -f "$JAR" ]]; then
  log "downloading Synthea ${SYNTHEA_VERSION}"
  curl -fsSL -o "$JAR.part" \
    "https://github.com/synthetichealth/synthea/releases/download/${SYNTHEA_VERSION}/synthea-with-dependencies.jar"
  mv "$JAR.part" "$JAR"
fi
actual="$(sha256sum "$JAR" | cut -d' ' -f1)"
expected="$(tr -d '[:space:]' < "$REPO_ROOT/packs/healthcare/generators/synthea.sha256")"
[[ -n "$SYNTHEA_SHA256" ]] && expected="$SYNTHEA_SHA256"
[[ "$actual" == "$expected" ]] || die "Synthea jar checksum mismatch: got $actual, expected $expected"

# Under the repo (gitignored), not mktemp: Docker Desktop cannot mount Git Bash /tmp paths.
work="$REPO_ROOT/data/tmp/synthea-$$"
rm -rf "$work" && mkdir -p "$work"
trap 'rm -rf "$work"' EXIT
log "generating $POPULATION patients (seed=$SEED, reference=$REFERENCE_DATE, end=$END_DATE)"
# -s population seed, -cs clinician seed, -r reference date, -e end date: all fixed, so the
# same release generates the same patients on any day.
# Hospital and practitioner bundles are not exported; only one bundle per patient.
# Run as the calling user so the output is not root-owned on Linux runners.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$CACHE_DIR:/synthea:ro" -v "$work:/out" -w /out \
  "$JAVA_IMAGE" java -Xmx1g -jar "/synthea/$(basename "$JAR")" \
  -p "$POPULATION" -s "$SEED" -cs "$SEED" -r "$REFERENCE_DATE" -e "$END_DATE" \
  --exporter.baseDirectory /out \
  --exporter.fhir.export true \
  --exporter.fhir.transaction_bundle true \
  --exporter.hospital.fhir.export false \
  --exporter.practitioner.fhir.export false \
  --exporter.csv.export false \
  --exporter.years_of_history 15 \
  --generate.only_alive_patients false \
  "$STATE" >"$work/synthea.log" 2>&1 || { tail -40 "$work/synthea.log" >&2; die "Synthea failed"; }

find "$work/fhir" -name '*.json' -exec cp {} "$OUT_DIR/" \;
log "wrote $(find "$OUT_DIR" -name '*.json' | wc -l | tr -d ' ') bundle(s) to $OUT_DIR"
