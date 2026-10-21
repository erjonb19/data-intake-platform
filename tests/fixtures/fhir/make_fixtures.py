"""Writes the hand-made FHIR fixtures in this folder (python tests/fixtures/fhir/make_fixtures.py).

These are NOT Synthea output. They are small, hand-written synthetic bundles shaped like
Synthea's FHIR R4 export (transaction bundles, urn:uuid references, SNOMED/LOINC/RxNorm
codes), so unit tests stay fast and readable. The person is fictional.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

HERE = Path(__file__).parent
PATIENT = "8f3e6b1a-0000-4000-8000-000000000001"


def _urn(i: str) -> str:
    return f"urn:uuid:{i}"


def _entry(resource: dict) -> dict:
    return {
        "fullUrl": _urn(resource["id"]),
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"]},
    }


def _encounter(n: int, start: str, end: str, cls: str = "AMB") -> dict:
    return {
        "resourceType": "Encounter",
        "id": f"enc-{n:04d}",
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls},
        "type": [
            {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "code": "162673000",
                        "display": "General examination of patient (procedure)",
                    }
                ]
            }
        ],
        "subject": {"reference": _urn(PATIENT)},
        "period": {"start": start, "end": end},
    }


def _hba1c(n: int, enc: str, when: str, value: float) -> dict:
    return {
        "resourceType": "Observation",
        "id": f"obs-{n:04d}",
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory",
                    }
                ]
            }
        ],
        "code": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": "4548-4",
                    "display": "Hemoglobin A1c/Hemoglobin.total in Blood",
                }
            ]
        },
        "subject": {"reference": _urn(PATIENT)},
        "encounter": {"reference": _urn(enc)},
        "effectiveDateTime": when,
        "valueQuantity": {
            "value": value,
            "unit": "%",
            "system": "http://unitsofmeasure.org",
            "code": "%",
        },
    }


def good_bundle() -> dict:
    encounters = [
        _encounter(1, "2018-03-02T09:00:00-05:00", "2018-03-02T09:30:00-05:00"),
        _encounter(2, "2025-02-10T10:00:00-05:00", "2025-02-10T10:20:00-05:00"),
        _encounter(3, "2025-08-18T14:00:00-04:00", "2025-08-18T14:25:00-04:00"),
    ]
    resources = [
        {
            "resourceType": "Patient",
            "id": PATIENT,
            "name": [{"use": "official", "family": "Testperson", "given": ["Fixture"]}],
            "gender": "female",
            "birthDate": "1961-07-14",
            "address": [{"city": "Bronx", "state": "NY", "postalCode": "10467"}],
            "extension": [
                {
                    "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                    "extension": [
                        {
                            "url": "ombCategory",
                            "valueCoding": {
                                "system": "urn:oid:2.16.840.1.113883.6.238",
                                "code": "2054-5",
                                "display": "Black or African American",
                            },
                        }
                    ],
                },
                {
                    "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
                    "extension": [
                        {
                            "url": "ombCategory",
                            "valueCoding": {
                                "system": "urn:oid:2.16.840.1.113883.6.238",
                                "code": "2186-5",
                                "display": "Not Hispanic or Latino",
                            },
                        }
                    ],
                },
            ],
        },
        *encounters,
        {
            "resourceType": "Condition",
            "id": "cond-0001",
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "code": "44054006",
                        "display": "Diabetes mellitus type 2 (disorder)",
                    }
                ]
            },
            "subject": {"reference": _urn(PATIENT)},
            "encounter": {"reference": _urn("enc-0001")},
            "onsetDateTime": "2018-03-02T09:00:00-05:00",
            "recordedDate": "2018-03-02T09:00:00-05:00",
        },
        _hba1c(1, "enc-0001", "2018-03-02T09:10:00-05:00", 7.9),
        _hba1c(2, "enc-0002", "2025-02-10T10:05:00-05:00", 7.1),
        _hba1c(3, "enc-0003", "2025-08-18T14:05:00-04:00", 6.8),
        {
            "resourceType": "MedicationRequest",
            "id": "medreq-0001",
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [
                    {
                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "860975",
                        "display": "24 HR metformin hydrochloride 500 MG Extended Release Oral Tablet",  # noqa: E501
                    }
                ]
            },
            "subject": {"reference": _urn(PATIENT)},
            "encounter": {"reference": _urn("enc-0001")},
            "authoredOn": "2018-03-02T09:20:00-05:00",
        },
        # Loaded types only; this one is counted and skipped by the contract.
        {
            "resourceType": "Claim",
            "id": "claim-0001",
            "status": "active",
            "patient": {"reference": _urn(PATIENT)},
        },
    ]
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [_entry(r) for r in resources],
    }


def bad_missing_birthdate() -> dict:
    b = copy.deepcopy(good_bundle())
    del b["entry"][0]["resource"]["birthDate"]
    return b


def main() -> None:
    files = {
        "good/patient_t2d.json": good_bundle(),
        "bad/patient_missing_birthdate.json": bad_missing_birthdate(),
        "bad/not_a_bundle.json": {"resourceType": "Patient", "id": "lonely"},
    }
    for name, doc in files.items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(doc, indent=2) + "\n").encode("utf-8"))
        print(f"wrote {path.relative_to(HERE)}")


if __name__ == "__main__":
    main()
