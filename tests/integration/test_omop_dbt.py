"""R2: FHIR bronze -> silver -> OMOP CDM with dbt, against a real Postgres.

Uses the hand-written fixture patient (known values), plus every Synthea bundle in
DIP_SYNTHEA_DIR when set. `dbt build` runs the models and all their dbt tests.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, vocab
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import PATIENT, good_bundle
from tests.integration.conftest import DSN, rows, scalar

FEED = "healthcare.fhir_patient_bundle"
CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))


@pytest.fixture(scope="module")
def omop(module_warehouse, tmp_path_factory):
    """Intake, vocabulary and one `dbt build`, shared by every test in this module."""
    warehouse = module_warehouse
    store = LocalStore(tmp_path_factory.mktemp("store"))
    store.put(BUCKET_LANDING, f"{FEED}/fixture.json", json.dumps(good_bundle()).encode())
    if synthea := os.environ.get("DIP_SYNTHEA_DIR"):
        for f in sorted(Path(synthea).glob("*.json")):
            store.put(BUCKET_LANDING, f"{FEED}/{f.name}", f.read_bytes())
    process_landing(store, warehouse, load_contracts(), datetime.now(UTC))
    vocab.load(warehouse.conn, vocab.fetch(CACHE))
    result = run_dbt(DSN, "build", check=False)
    assert result.returncode == 0, "dbt build failed (models or tests); see output above"
    return warehouse


def person_id(wh):
    return scalar(
        wh, "select person_id from omop.person where person_source_value = %s", (PATIENT,)
    )


def test_person_demographics(omop):
    gender, race, ethnicity, yob = rows(
        omop,
        "select gender_concept_id, race_concept_id, ethnicity_concept_id, year_of_birth"
        " from omop.person where person_source_value = %s",
        (PATIENT,),
    )[0]
    assert (gender, race, ethnicity, yob) == (8532, 8516, 38003564, 1961)


def test_type2_diabetes_maps_to_standard_concept(omop):
    assert rows(
        omop,
        "select condition_concept_id, condition_start_date, condition_source_value"
        " from omop.condition_occurrence where person_id = %s",
        (person_id(omop),),
    ) == [(201826, date(2018, 3, 2), "44054006")]


def test_hba1c_measurements(omop):
    got = rows(
        omop,
        "select measurement_date, value_as_number, unit_concept_id from omop.measurement"
        " where person_id = %s and measurement_concept_id = 3004410 order by 1",
        (person_id(omop),),
    )
    assert [(d, float(v), u) for d, v, u in got] == [
        (date(2018, 3, 2), 7.9, 8554),
        (date(2025, 2, 10), 7.1, 8554),
        (date(2025, 8, 18), 6.8, 8554),
    ]


def test_visits_drugs_and_observation_period(omop):
    pid = person_id(omop)
    assert rows(
        omop,
        "select visit_concept_id, count(*) from omop.visit_occurrence where person_id = %s"
        " group by 1",
        (pid,),
    ) == [(9202, 3)]
    assert rows(
        omop, "select drug_concept_id from omop.drug_exposure where person_id = %s", (pid,)
    ) == [(40163924,)]
    assert rows(
        omop,
        "select observation_period_start_date, observation_period_end_date"
        " from omop.observation_period where person_id = %s",
        (pid,),
    ) == [(date(2018, 3, 2), date(2025, 8, 18))]


def test_ids_are_stable_across_rebuilds(omop):
    before = person_id(omop)
    assert run_dbt(DSN, "run", "--select", "person", check=False).returncode == 0
    assert person_id(omop) == before


@pytest.mark.skipif(not os.environ.get("DIP_SYNTHEA_DIR"), reason="DIP_SYNTHEA_DIR not set")
def test_synthea_mapping_report(omop):
    """Prints how much of real Synthea output the vocabulary shard maps (for the docs)."""
    for table, concept in [
        ("condition_occurrence", "condition_concept_id"),
        ("measurement", "measurement_concept_id"),
        ("drug_exposure", "drug_concept_id"),
        ("visit_occurrence", "visit_concept_id"),
    ]:
        total, unmapped = rows(
            omop, f"select count(*), count(*) filter (where {concept} = 0) from omop.{table}"
        )[0]
        print(f"{table:<22} rows={total:<7} unmapped={unmapped / max(total, 1):.1%}")
        assert total > 0
    assert scalar(omop, "select count(*) from omop.person") > 1


def test_vocab_needs_load(omop):
    assert vocab.needs_load(omop.conn) is False
