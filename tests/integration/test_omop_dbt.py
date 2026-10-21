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


def test_dq_results_recorded(omop):
    """R3: every test of the build is in ops.dq_results; none failed (warnings allowed)."""
    summary = rows(
        omop,
        "select dq_category, status, count(*) from ops.dq_results"
        " where invocation_id = (select invocation_id from ops.dq_results"
        "   order by recorded_at limit 1)"
        " group by 1, 2 order by 1, 2",
    )
    print("DQ summary (category, status, tests):", summary)
    categories = {c for c, _, _ in summary}
    assert {"conformance", "completeness", "plausibility", "integrity"} <= categories
    assert not [s for _, s, _ in summary if s not in ("pass", "warn")], summary
    warned = rows(
        omop,
        "select test_name, failures from ops.dq_results where status = 'warn' order by 1",
    )
    print("DQ warnings:", warned)


def test_dq_catches_implausible_hba1c(omop):
    """R3 negative test: an HbA1c of 55% fails plausibleValueHigh and is recorded."""
    with omop.conn.cursor() as cur:
        cur.execute(
            "insert into omop.measurement (measurement_id, person_id, measurement_concept_id,"
            " measurement_date, measurement_type_concept_id, value_as_number, unit_concept_id)"
            " select -1, person_id, 3004410, measurement_date, 32817, 55, 8554"
            " from omop.measurement where measurement_concept_id = 3004410 limit 1"
        )
    omop.conn.commit()
    try:
        result = run_dbt(DSN, "test", "--select", "plausible_value_hba1c", check=False)
        assert result.returncode != 0
        assert rows(
            omop,
            "select status, failures from ops.dq_results where test_name = 'plausible_value_hba1c'"
            " order by recorded_at desc limit 1",
        ) == [("fail", 1)]
    finally:
        with omop.conn.cursor() as cur:
            cur.execute("delete from omop.measurement where measurement_id = -1")
        omop.conn.commit()
