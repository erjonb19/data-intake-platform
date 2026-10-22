"""The claims generator (S2.4): billed lines only, the plan's dates only, member ids that
match the eligibility generator's, and output the contract accepts. The EOB is invented,
in Synthea's shape."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime

from dip.contracts import REPO_ROOT, load_contracts
from dip.csvfeed import validate_csv

PATIENT = "8f3e6b1a-0000-4000-8000-000000000001"
BB = "https://bluebutton.cms.gov/resources/variables/"


def _load(name):
    path = REPO_ROOT / f"packs/healthcare/generators/{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(seq, start, code, system, amounts=None):
    item = {
        "sequence": seq,
        "productOrService": {"coding": [{"system": system, "code": code}]},
        "servicedPeriod": {"start": start, "end": start},
        "locationCodeableConcept": {"coding": [{"code": "19"}]},
    }
    if amounts:
        item["adjudication"] = [
            {"category": {"coding": [{"code": BB + k}]}, "amount": {"value": v}}
            for k, v in amounts.items()
        ]
    return item


AMOUNTS = {
    "line_sbmtd_chrg_amt": 136.0,
    "line_alowd_chrg_amt": 136.0,
    "line_prvdr_pmt_amt": 108.8,
    "line_coinsrnc_amt": 27.2,
    "line_bene_ptb_ddctbl_amt": 0.0,
}


def bundle():
    eob = {
        "resourceType": "ExplanationOfBenefit",
        "id": "eob-1",
        "claim": {"reference": "urn:uuid:00000000-0000-4000-8000-0000000000aa"},
        "patient": {"reference": f"urn:uuid:{PATIENT}"},
        "insurance": [{"focal": True, "coverage": {"display": "Invented Health"}}],
        "item": [
            _item(1, "2023-05-30T10:00:00+00:00", "185349003", "http://snomed.info/sct"),
            _item(2, "2023-05-30T10:00:00+00:00", "140", "http://hl7.org/fhir/sid/cvx", AMOUNTS),
            _item(3, "2019-12-31T10:00:00+00:00", "4548-4", "http://loinc.org", AMOUNTS),
            _item(4, "2024-01-02T10:00:00+00:00", "X1", "http://example.org/local", AMOUNTS),
        ],
    }
    patient = {"resourceType": "Patient", "id": PATIENT}
    return {"resourceType": "Bundle", "entry": [{"resource": patient}, {"resource": eob}]}


def test_billed_lines_from_the_plan_start(tmp_path):
    (tmp_path / "p.json").write_text(json.dumps(bundle()), encoding="utf-8")
    gen = _load("claims")
    rows = gen.claim_rows(tmp_path)
    # Line 1 has no adjudication (not billed); line 3 predates the plan.
    assert [(r["line_number"], r["procedure_system"]) for r in rows] == [
        ("2", "CVX"),
        ("4", "LOCAL"),
    ]
    assert rows[0] == {
        "claim_line_id": "00000000-0000-4000-8000-0000000000aa-2",
        "claim_id": "00000000-0000-4000-8000-0000000000aa",
        "line_number": "2",
        "member_id": rows[0]["member_id"],
        "service_date": "2023-05-30",
        "place_of_service": "19",
        "procedure_code": "140",
        "procedure_system": "CVX",
        "billed_amount": "136.00",
        "allowed_amount": "136.00",
        "paid_amount": "108.80",
        "coinsurance_amount": "27.20",
        "deductible_amount": "0.00",
        "payer": "Invented Health",
    }


def test_member_ids_match_eligibility():
    elig = _load("eligibility")
    member = elig.member_row({"id": PATIENT, "birthDate": "1960-01-01", "name": []})
    assert _load("claims").member_id(PATIENT) == member["member_id"]


def test_output_is_deterministic_and_valid(tmp_path):
    (tmp_path / "p.json").write_text(json.dumps(bundle()), encoding="utf-8")
    gen = _load("claims")
    first = gen.claims_csv(tmp_path)
    assert first == gen.claims_csv(tmp_path)
    contract = load_contracts()["healthcare.claims"]
    r = validate_csv(first, contract, datetime(2026, 10, 22, tzinfo=UTC))
    assert not r.file_violations and not r.rejected and len(r.accepted) == 2
