from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest

from dip.contracts import load_contracts
from dip.fhir import validate_bundle
from tests.fixtures.fhir.make_fixtures import good_bundle

RECEIVED = datetime(2026, 10, 21, tzinfo=UTC)


@pytest.fixture
def contract():
    return load_contracts()["healthcare.fhir_patient_bundle"]


def run(bundle, contract):
    return validate_bundle(json.dumps(bundle).encode(), contract, RECEIVED)


def find(bundle, rtype, rid=None):
    return next(
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == rtype and (rid is None or e["resource"]["id"] == rid)
    )


def test_good_bundle_loads_everything_and_skips_claims(contract):
    r = run(good_bundle(), contract)
    assert not r.file_rejected
    assert not r.rejected
    assert r.skipped == {"Claim": 1}
    assert len(r.accepted) == 9
    assert r.patient_id == "8f3e6b1a-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xff\xfe not json", "FILE.INVALID_JSON"),
        (b"{not json", "FILE.INVALID_JSON"),
        (b'{"resourceType": "Patient"}', "FHIR.NOT_A_BUNDLE"),
        (b"[]", "FHIR.NOT_A_BUNDLE"),
        (
            b'{"resourceType": "Bundle", "type": "searchset", "entry": [{}]}',
            "FHIR.BUNDLE_TYPE:searchset",
        ),
        (b'{"resourceType": "Bundle", "type": "transaction", "entry": []}', "FHIR.EMPTY_BUNDLE"),
    ],
)
def test_structural_failures_reject_file(contract, data, code):
    r = validate_bundle(data, contract, RECEIVED)
    assert [v.reason_code for v in r.file_violations] == [code]
    assert not r.accepted


def test_invalid_patient_rejects_whole_file(contract):
    b = good_bundle()
    del find(b, "Patient")["birthDate"]
    r = run(b, contract)
    assert [v.reason_code for v in r.file_violations] == [
        "FHIR.PATIENT_INVALID:FHIR.MISSING_FIELD:Patient.birthDate"
    ]


def test_two_patients_rejects_file(contract):
    b = good_bundle()
    other = copy.deepcopy(b["entry"][0])
    other["resource"]["id"] = "someone-else"
    other["fullUrl"] = "urn:uuid:someone-else"
    b["entry"].append(other)
    assert [v.reason_code for v in run(b, contract).file_violations] == ["FHIR.PATIENT_COUNT:2"]


def test_bad_gender_value(contract):
    b = good_bundle()
    find(b, "Patient")["gender"] = "F"
    codes = [v.reason_code for v in run(b, contract).file_violations]
    assert codes == ["FHIR.PATIENT_INVALID:FHIR.INVALID_VALUE:Patient.gender"]


def _ratio_tolerant(contract):
    spec = copy.deepcopy(contract.spec)
    spec["max_bad_row_ratio"] = 0.5
    return type(contract)(contract.feed, contract.version, spec, contract.path)


def test_rejected_encounter_cascades_to_its_records(contract):
    b = good_bundle()
    del find(b, "Encounter", "enc-0001")["period"]
    r = run(b, _ratio_tolerant(contract))
    rejected = {(res.resource_id, v.reason_code) for res, v in r.rejected}
    assert rejected == {
        ("enc-0001", "FHIR.MISSING_FIELD:Encounter.period.start"),
        ("cond-0001", "FHIR.UNRESOLVED_REFERENCE:Condition.encounter"),
        ("obs-0001", "FHIR.UNRESOLVED_REFERENCE:Observation.encounter"),
        ("medreq-0001", "FHIR.UNRESOLVED_REFERENCE:MedicationRequest.encounter"),
    }
    assert {res.resource_id for res in r.accepted} >= {"enc-0002", "obs-0002"}


def test_too_many_bad_records_rejects_file(contract):
    b = good_bundle()
    del find(b, "Encounter", "enc-0001")["period"]  # 4 of 9 resources rejected
    codes = [v.reason_code for v in run(b, contract).file_violations]
    assert codes == ["CONTRACT.BAD_ROW_RATIO"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda o: o.update(effectiveDateTime="2025-13-45"),
            "FHIR.INVALID_DATE:Observation.effectiveDateTime",
        ),
        (
            lambda o: o.update(effectiveDateTime="2031-01-01T00:00:00Z"),
            "FHIR.FUTURE_DATE:Observation.effectiveDateTime",
        ),
        (
            lambda o: o["code"]["coding"][0].update(system="http://example.org/local"),
            "FHIR.UNKNOWN_CODE_SYSTEM:Observation.code",
        ),
        (
            lambda o: o.update(subject={"reference": "urn:uuid:nobody"}),
            "FHIR.UNRESOLVED_REFERENCE:Observation.subject",
        ),
        (lambda o: o.pop("status"), "FHIR.MISSING_FIELD:Observation.status"),
    ],
)
def test_record_level_violations(contract, mutate, code):
    b = good_bundle()
    mutate(find(b, "Observation", "obs-0003"))
    r = run(b, _ratio_tolerant(contract))
    assert not r.file_rejected
    assert [(res.resource_id, v.reason_code) for res, v in r.rejected] == [("obs-0003", code)]
    assert len(r.accepted) == 8


def test_duplicate_resource_id(contract):
    b = good_bundle()
    dup = copy.deepcopy(b["entry"][5])
    b["entry"].append(dup)
    r = run(b, _ratio_tolerant(contract))
    assert [v.reason_code for _, v in r.rejected] == ["FHIR.DUPLICATE_ID:Observation"]


def test_reject_file_mode(contract):
    spec = copy.deepcopy(contract.spec)
    spec["on_row_violation"] = "reject_file"
    strict = type(contract)(contract.feed, contract.version, spec, contract.path)
    b = good_bundle()
    find(b, "Observation", "obs-0003").pop("status")
    assert [v.reason_code for v in run(b, strict).file_violations] == ["CONTRACT.ROW_VIOLATION"]
