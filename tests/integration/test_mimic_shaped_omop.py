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
    assert rows(omop, "select drug_source_value from omop.drug_exposure order by 1") == [
        ("00000000001",),
        ("mix-1",),
    ]


def test_shifted_birth_year_kept(omop):
    assert rows(omop, "select year_of_birth from omop.person") == [(2091,)]
