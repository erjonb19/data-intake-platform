"""A tiny FHIR Bulk Data export in the SHAPE of MIMIC-IV on FHIR, for CI.

NOT MIMIC data. Every id, code choice, value and date here is invented by hand. It copies
only MIMIC's structure: one NDJSON file per resource type, "Type/id" references across
files, MIMIC-local code system URIs, dates shifted into the 2100s, conditions without an
onset, hospital/ED/ICU encounters told apart by identifier system, and orders that point
at NDC-coded Medications or at IV mixes with ingredients only.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

MIMIC = "http://mimic.mit.edu/fhir/mimic"
PATIENT = "00000000-0000-4000-8000-00000000a001"
HOSP, ED, ICU = (
    "00000000-0000-4000-8000-00000000e001",
    "00000000-0000-4000-8000-00000000e002",
    "00000000-0000-4000-8000-00000000e003",
)
NDC_MED, MIX_MED = "00000000-0000-4000-8000-00000000d001", "00000000-0000-4000-8000-00000000d002"


def _ref(kind: str, rid: str) -> dict:
    return {"reference": f"{kind}/{rid}"}


def _encounter(rid, kind, cls, start, end, part_of=None):
    e = {
        "resourceType": "Encounter",
        "id": rid,
        "meta": {"profile": [f"{MIMIC}/StructureDefinition/mimic-encounter"]},
        "identifier": [{"system": f"{MIMIC}/identifier/encounter-{kind}", "value": f"9{kind}01"}],
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls},
        "subject": _ref("Patient", PATIENT),
        "period": {"start": start, "end": end},
    }
    if part_of:
        e["partOf"] = _ref("Encounter", part_of)
    return e


def export() -> dict[str, list[dict]]:
    return {
        "MimicPatient": [
            {
                "resourceType": "Patient",
                "id": PATIENT,
                "identifier": [{"system": f"{MIMIC}/identifier/patient", "value": "90000001"}],
                "gender": "female",
                "birthDate": "2091-05-01",
            }
        ],
        "MimicEncounter": [
            _encounter(
                HOSP, "hosp", "EMER", "2155-03-01T10:00:00-05:00", "2155-03-06T12:00:00-05:00"
            )
        ],
        "MimicEncounterED": [
            _encounter(
                ED, "ed", "EMER", "2155-03-01T04:00:00-05:00", "2155-03-01T10:00:00-05:00", HOSP
            )
        ],
        "MimicEncounterICU": [
            _encounter(
                ICU, "icu", "ACUTE", "2155-03-02T00:00:00-05:00", "2155-03-04T00:00:00-05:00", HOSP
            )
        ],
        "MimicCondition": [
            {
                "resourceType": "Condition",
                "id": f"00000000-0000-4000-8000-0000000c000{i}",
                "subject": _ref("Patient", PATIENT),
                "encounter": _ref("Encounter", HOSP),
                "code": {
                    "coding": [{"system": f"{MIMIC}/CodeSystem/mimic-diagnosis-{v}", "code": c}]
                },
            }
            for i, (v, c) in enumerate([("icd9", "4019"), ("icd10", "E119")], start=1)
        ],
        "MimicObservationLabevents": [
            {
                "resourceType": "Observation",
                "id": "00000000-0000-4000-8000-0000000b0001",
                "status": "final",
                "category": [{"coding": [{"code": "laboratory"}]}],
                "code": {
                    "coding": [{"system": f"{MIMIC}/CodeSystem/mimic-d-labitems", "code": "99001"}]
                },
                "subject": _ref("Patient", PATIENT),
                "encounter": _ref("Encounter", ICU),
                "effectiveDateTime": "2155-03-02T06:00:00-05:00",
                "valueQuantity": {"value": 7.2, "unit": "%"},
            }
        ],
        "MimicMedication": [
            {
                "resourceType": "Medication",
                "id": NDC_MED,
                "code": {
                    "coding": [
                        {
                            "system": f"{MIMIC}/CodeSystem/mimic-medication-ndc",
                            "code": "00000000001",
                        }
                    ]
                },
            }
        ],
        "MimicMedicationMix": [
            {
                "resourceType": "Medication",
                "id": MIX_MED,
                "identifier": [{"system": f"{MIMIC}/identifier/medication-mix", "value": "mix-1"}],
                "ingredient": [{"itemReference": _ref("Medication", NDC_MED)}],
            }
        ],
        "MimicMedicationRequest": [
            {
                "resourceType": "MedicationRequest",
                "id": f"00000000-0000-4000-8000-0000000f000{i}",
                "status": "completed",
                "intent": "order",
                "subject": _ref("Patient", PATIENT),
                "encounter": _ref("Encounter", HOSP),
                "authoredOn": "2155-03-02T08:00:00-05:00",
                "medicationReference": _ref("Medication", med),
            }
            for i, med in enumerate([NDC_MED, MIX_MED], start=1)
        ],
    }


def write(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in export().items():
        path = dest / f"{name}.ndjson.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        paths.append(path)
    return paths
