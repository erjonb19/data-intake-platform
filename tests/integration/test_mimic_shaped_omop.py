"""M1: the MIMIC-shaped synthetic export through intake and dbt into OMOP.

Checks the MIMIC-specific mapping rules on known values: hospital encounters are inpatient
and ED encounters ER visits (told apart by identifier system), ICU stays fold into their
hospital visit, ICD codes get their dot, diagnoses take the encounter start as their date,
and IV mixes keep a source value.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, vocab
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.fhir_bulk import bundle_export
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.mimic_shaped.make_fixture import write
from tests.integration.conftest import DSN, _fresh_warehouse, rows

CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
CONTRACTS = load_contracts((REPO_ROOT / "packs/mimic/contracts",))
LACTATE = 40762125  # LOINC 59032-3, in the Eunomia shard
FACE_MASK = 4126216  # SNOMED 261352009, domain Device, in the Eunomia shard


@pytest.fixture(scope="module")
def omop(tmp_path_factory):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    root = tmp_path_factory.mktemp("mimic_shaped")
    wh = _fresh_warehouse()
    store = LocalStore(root / "store")
    for pid, data in bundle_export(write(root / "export"), root / "spool"):
        store.put(BUCKET_LANDING, f"mimic.fhir_demo/{pid}.json", data)
    [outcome] = process_landing(store, wh, CONTRACTS, datetime(2026, 10, 21, tzinfo=UTC))
    assert outcome.status == "loaded", outcome
    vocab.load(wh.conn, vocab.fetch(CACHE))
    # A site map for the invented lab item, to a LOINC concept the Eunomia shard has.
    vocab.load_source_to_concept_map(
        wh.conn,
        vocab.MIMIC_LAB_VOCABULARY,
        [("99001", 0, "MIMIC_LAB", "Invented lab A", LACTATE, "LOINC", "1970-01-01",
          "2099-12-31", None)],
    )  # fmt: skip
    # The invented NDC maps (by a site map) to a Device concept: a device, not a drug.
    vocab.load_source_to_concept_map(
        wh.conn,
        "NDC",
        [("00000000001", 0, "NDC", "Invented device", FACE_MASK, "SNOMED", "1970-01-01",
          "2099-12-31", None)],
    )  # fmt: skip
    install_key(wh.conn, "ci-test-key-0123456789abcdef0123456789abcdef")
    # Models only: the year-of-birth check rightly fails on MIMIC's shifted birth dates.
    assert run_dbt(DSN, "seed", check=False).returncode == 0
    assert run_dbt(DSN, "run", check=False).returncode == 0
    yield wh
    wh.conn.close()


def test_visits_by_identifier_system_and_icu_folded(omop):
    # Three encounters, two visits: hospital (inpatient) and ED (ER); the ICU stay is not one.
    assert rows(
        omop,
        "select visit_concept_id, visit_start_datetime::text from omop.visit_occurrence order by 1",
    ) == [(9201, "2155-03-01 15:00:00"), (9203, "2155-03-01 09:00:00")]
    # The lab was taken during the ICU stay: it belongs to the hospital (inpatient) visit.
    assert rows(
        omop,
        "select v.visit_concept_id from omop.measurement m"
        " join omop.visit_occurrence v using (visit_occurrence_id)",
    ) == [(9201,)]


def test_conditions_dated_by_encounter_with_dotted_icd(omop):
    assert rows(
        omop,
        "select condition_source_value, condition_start_date, condition_concept_id"
        " from omop.condition_occurrence order by 1",
    ) == [("401.9", date(2155, 3, 1), 0), ("E11.9", date(2155, 3, 1), 0)]


def test_drug_source_values_for_ndc_and_iv_mix(omop):
    # The NDC that maps to a Device concept stays a drug row with concept 0 (there is no
    # device_exposure table here), so it is counted, not dropped.
    assert rows(
        omop, "select drug_source_value, drug_concept_id from omop.drug_exposure order by 1"
    ) == [("00000000001", 0), ("mix-1", 0)]


def test_shifted_birth_year_kept(omop):
    assert rows(omop, "select year_of_birth from omop.person") == [(2091,)]


def test_lab_item_mapped_through_source_to_concept_map(omop):
    assert rows(
        omop,
        "select measurement_source_value, measurement_concept_id, measurement_source_concept_id"
        " from omop.measurement",
    ) == [("99001", LACTATE, 0)]


def test_observation_without_category_is_not_dropped(omop):
    """An unmapped Observation with no category goes to `observation`: the measurement and
    observation filters are exact complements even when category is NULL."""
    with omop.conn.cursor() as cur:
        cur.execute(
            "insert into bronze.fhir_resource (feed, file_sha256, entry_index, resource_type,"
            " resource_id, patient_id, resource, loaded_at)"
            " select feed, 'test-no-category', 0, 'Observation', 'obs-no-category', patient_id,"
            "   (resource - 'category') || jsonb_build_object('id', 'obs-no-category',"
            "     'code', jsonb_build_object('coding', jsonb_build_array(jsonb_build_object("
            "       'system', 'http://example.org/invented', 'code', 'no-category-1')))),"
            "   loaded_at"
            " from bronze.fhir_resource where resource_type = 'Observation' limit 1"
        )
    omop.conn.commit()
    try:
        assert (
            run_dbt(DSN, "run", "--select", "fhir_resource_current+", check=False).returncode == 0
        )
        assert rows(
            omop,
            "select observation_concept_id from omop.observation"
            " where observation_source_value = 'no-category-1'",
        ) == [(0,)]
        assert rows(
            omop,
            "select count(*) from omop.measurement"
            " where measurement_source_value = 'no-category-1'",
        ) == [(0,)]
        assert (
            run_dbt(DSN, "test", "--select", "no_source_rows_dropped", check=False).returncode == 0
        )
    finally:
        with omop.conn.cursor() as cur:
            cur.execute("delete from bronze.fhir_resource where file_sha256 = 'test-no-category'")
        omop.conn.commit()
        assert (
            run_dbt(DSN, "run", "--select", "fhir_resource_current+", check=False).returncode == 0
        )
