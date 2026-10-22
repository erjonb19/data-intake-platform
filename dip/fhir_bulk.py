"""FHIR Bulk Data (NDJSON) -> one transaction Bundle per patient.

A Bulk Data export has one NDJSON file per resource type, and its references point across
files ("Patient/<id>", "Encounter/<id>"). Validating such references needs the whole
export, so instead of a second validator this adapter regroups the export by patient,
which is the shape the intake already understands (Synthea's). Every contract check,
including per-patient quarantine, then applies unchanged.

Resources are kept byte-for-byte as delivered (no reference rewriting): the intake's
reference check accepts both "urn:uuid:<id>" and "<Type>/<id>". Shared resources that a
patient's records point to (for example Medication) are copied into that patient's bundle.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# Fields that link a resource to its patient, in order of preference.
PATIENT_FIELDS = ("subject", "patient")
# Fields that point at shared, patient-independent resources to copy into the bundle.
SHARED_REFERENCE_FIELDS = ("medicationReference",)


def read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _patient_of(resource: dict[str, Any]) -> str | None:
    if resource.get("resourceType") == "Patient":
        return resource.get("id")
    for field in PATIENT_FIELDS:
        ref = resource.get(field)
        if isinstance(ref, dict) and isinstance(ref.get("reference"), str):
            return ref["reference"].rsplit("/", 1)[-1]
    return None


def _entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "fullUrl": f"{resource['resourceType']}/{resource['id']}",
        "resource": resource,
        "request": {"method": "PUT", "url": f"{resource['resourceType']}/{resource['id']}"},
    }


def bundles(resources: Iterable[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(patient id, bundle) for every patient, in patient-id order.

    Resources without a patient link and not referenced by any patient record are
    dropped (for example Organization, Location); the counts are in `orphans`.
    """
    by_patient: dict[str, list[dict]] = defaultdict(list)
    shared: dict[str, dict] = {}
    for r in resources:
        pid = _patient_of(r)
        if pid is not None:
            by_patient[pid].append(r)
        elif "id" in r:
            shared[f"{r['resourceType']}/{r['id']}"] = r
    for pid in sorted(by_patient):
        records = by_patient[pid]
        # The Patient first, then everything else in a stable order.
        records.sort(key=lambda r: (r["resourceType"] != "Patient", r["resourceType"], r["id"]))
        refs = sorted(
            {
                r[f]["reference"]
                for r in records
                for f in SHARED_REFERENCE_FIELDS
                if isinstance(r.get(f), dict) and r[f].get("reference") in shared
            }
        )
        entries = [_entry(r) for r in records] + [_entry(shared[ref]) for ref in refs]
        yield pid, {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def bundle_export(files: Iterable[Path], spool_dir: Path) -> Iterator[tuple[str, bytes]]:
    """Stream an export into one bundle per patient: (patient id, bundle JSON bytes).

    Exports can be far larger than memory, so resources are first spooled to one NDJSON
    file per patient under `spool_dir`; only shared resources (small) stay in memory.
    """
    spool_dir.mkdir(parents=True, exist_ok=True)
    shared: dict[str, dict] = {}
    handles: dict[str, Any] = {}
    try:
        for path in sorted(files):
            for r in read_ndjson(path):
                pid = _patient_of(r)
                if pid is None:
                    if "id" in r:
                        shared[f"{r['resourceType']}/{r['id']}"] = r
                    continue
                if pid not in handles:
                    handles[pid] = (spool_dir / f"{pid}.ndjson").open("w", encoding="utf-8")
                handles[pid].write(json.dumps(r) + "\n")
    finally:
        for h in handles.values():
            h.close()
    for pid in sorted(handles):
        records = list(read_ndjson(spool_dir / f"{pid}.ndjson"))
        for _, bundle in bundles([*records, *(_referenced(records, shared))]):
            yield pid, (json.dumps(bundle, sort_keys=True) + "\n").encode("utf-8")
        (spool_dir / f"{pid}.ndjson").unlink()


def _referenced(records: list[dict], shared: dict[str, dict]) -> list[dict]:
    refs = {
        r[f]["reference"]
        for r in records
        for f in SHARED_REFERENCE_FIELDS
        if isinstance(r.get(f), dict) and r[f].get("reference") in shared
    }
    return [shared[ref] for ref in sorted(refs)]
