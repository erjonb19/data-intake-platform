"""M1: Bulk Data regrouping and the MIMIC contract, on a synthetic MIMIC-shaped export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import REPO_ROOT, load_contracts
from dip.fhir import validate_bundle
from dip.fhir_bulk import bundle_export
from dip.intake import process_landing
from dip.storage import LocalStore
from dip.warehouse import DryRunWarehouse
from tests.fixtures.mimic_shaped.make_fixture import MIX_MED, NDC_MED, PATIENT, write

MIMIC = load_contracts((REPO_ROOT / "packs/mimic/contracts",))["mimic.fhir_demo"]
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _bundles(tmp_path: Path):
    files = write(tmp_path / "export")
    return list(bundle_export(files, tmp_path / "spool"))


def test_one_bundle_per_patient_with_shared_medications(tmp_path):
    [(pid, data)] = _bundles(tmp_path)
    assert pid == PATIENT
    bundle = json.loads(data)
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types[0] == "Patient"
    assert types.count("Encounter") == 3
    meds = {
        e["resource"]["id"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Medication"
    }
    assert meds == {NDC_MED, MIX_MED}
    assert not list((tmp_path / "spool").glob("*.ndjson"))  # spool cleaned up


def test_regrouping_is_deterministic(tmp_path):
    assert _bundles(tmp_path / "a") == _bundles(tmp_path / "b")


def test_mimic_contract_accepts_the_shape(tmp_path):
    [(_, data)] = _bundles(tmp_path)
    r = validate_bundle(data, MIMIC, NOW)
    assert not r.file_violations
    assert not r.rejected
    assert len(r.accepted) == 11


def test_synthea_contract_rejects_the_shape(tmp_path):
    """What breaks: shifted dates are 'in the future' for the Synthea contract."""
    [(_, data)] = _bundles(tmp_path)
    r = validate_bundle(data, load_contracts()["healthcare.fhir_patient_bundle"], NOW)
    assert [v.reason_code for v in r.file_violations] == [
        "FHIR.PATIENT_INVALID:FHIR.FUTURE_DATE:Patient.birthDate"
    ]


def test_date_window_rejects_dates_outside_it(tmp_path):
    [(_, data)] = _bundles(tmp_path)
    bundle = json.loads(data)
    lab = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    )
    lab["effectiveDateTime"] = "2300-01-01T00:00:00Z"
    spec = json.loads(json.dumps(MIMIC.spec))
    spec["max_bad_row_ratio"] = 1.0
    contract = type(MIMIC)(MIMIC.feed, MIMIC.version, spec, MIMIC.path)
    r = validate_bundle(json.dumps(bundle).encode(), contract, NOW)
    assert [v.reason_code for _, v in r.rejected] == [
        "FHIR.DATE_OUT_OF_WINDOW:Observation.effectiveDateTime"
    ]


def test_raw_ndjson_as_delivered_is_quarantined_by_name(tmp_path):
    store = LocalStore(tmp_path / "store")
    for path in write(tmp_path / "export"):
        store.put(BUCKET_LANDING, f"mimic.fhir_demo/{path.name}", path.read_bytes())
    outcomes = process_landing(store, DryRunWarehouse(), {"mimic.fhir_demo": MIMIC}, NOW)
    assert {o.reason_codes for o in outcomes} == {("FILE.NAME_PATTERN",)}
