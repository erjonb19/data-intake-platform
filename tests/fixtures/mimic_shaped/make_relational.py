"""Tiny relational tables in the SHAPE of the MIMIC-IV demo, for CI.

NOT MIMIC data: every value is invented by hand. They describe the same invented patient
as make_fixture.py (the FHIR-shaped export), so the M3 reconciliation can be tested too,
with two deliberate differences that a reconciliation must explain:
  - birth date: the relational tables only know anchor_age at anchor_year (a year), while
    the FHIR Patient has a full date
  - one extra lab result exists only in the relational tables
The column lists are exactly the demo's, so the relational contract applies unchanged.
"""

from __future__ import annotations

from pathlib import Path

from dip.relational import csv_bytes, load_contract
from tests.fixtures.mimic_shaped.make_fixture import ENC_IDS, SUBJECT_ID

CONTRACT = load_contract(
    Path(__file__).resolve().parents[3] / "packs/mimic/relational/relational_demo.yaml"
)
HADM = ENC_IDS["hosp"]


def rows() -> dict[str, list[dict]]:
    return {
        "patients": [
            {
                "subject_id": SUBJECT_ID,
                "gender": "F",
                "anchor_age": 64,
                "anchor_year": 2155,
                "anchor_year_group": "2017 - 2019",
            },
        ],
        "admissions": [
            {
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "admittime": "2155-03-01 10:00:00",
                "dischtime": "2155-03-06 12:00:00",
                "admission_type": "EW EMER.",
                "race": "WHITE",
            },
        ],
        "diagnoses_icd": [
            {
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "seq_num": 1,
                "icd_code": "4019",
                "icd_version": 9,
            },
            {
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "seq_num": 2,
                "icd_code": "E119",
                "icd_version": 10,
            },
        ],
        "labevents": [
            {
                "labevent_id": 1,
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "itemid": 99001,
                "charttime": "2155-03-02 06:00:00",
                "valuenum": 7.2,
                "valueuom": "%",
                "value": "7.2",
            },
            {
                "labevent_id": 2,
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "itemid": 99002,
                "charttime": "2155-03-03 06:00:00",
                "valuenum": 1.1,
                "valueuom": "mg/dL",
                "value": "1.1",
            },
        ],
        "prescriptions": [
            {
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "pharmacy_id": 1,
                "starttime": "2155-03-02 08:00:00",
                "drug": "Drug A",
                "ndc": "00000000001",
            },
            {
                "subject_id": SUBJECT_ID,
                "hadm_id": HADM,
                "pharmacy_id": 2,
                "starttime": "2155-03-02 08:00:00",
                "drug": "Mix 1",
                "ndc": "0",
            },
        ],
        "d_labitems": [
            {"itemid": 99001, "label": "Invented lab A", "fluid": "Blood", "category": "Chemistry"},
            {"itemid": 99002, "label": "Invented lab B", "fluid": "Blood", "category": "Chemistry"},
        ],
    }


def write(root: Path) -> Path:
    """Gzipped CSVs laid out like the demo (hosp/<table>.csv.gz); returns root."""
    for table, table_rows in rows().items():
        spec = CONTRACT["tables"][table]
        columns = list(spec["columns"])
        path = root / spec["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(csv_bytes(columns, [[r.get(c, "") for c in columns] for r in table_rows]))
    return root
