"""R7: the cohort library run as the read-only cohort reader on the shareable tier.

Builds the hand-made fixture patient (type 2 diabetes in 2018, HbA1c 7.9% in 2018, 7.1% and
6.8% in 2025) through intake and dbt, so every count is known: one person passes every
step. Released counts of one are suppressed, so the logic is checked on the raw query too.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, cohorts, vocab
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import good_bundle
from tests.integration.conftest import DSN, rows
from tests.integration.test_cohort_reader import READER_PASSWORD, add_feed, reader_dsn, remove_feed

FEED = "healthcare.fhir_patient_bundle"
CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))


@pytest.fixture(scope="module")
def tier(module_warehouse, tmp_path_factory):
    wh = module_warehouse
    with wh.conn.cursor() as cur:
        cur.execute((REPO_ROOT / "infra/terraform/files/postgres-roles.sql").read_text("utf-8"))
        cur.execute(f"alter role dip_cohort_reader password '{READER_PASSWORD}'")
    wh.conn.commit()
    store = LocalStore(tmp_path_factory.mktemp("store"))
    store.put(BUCKET_LANDING, f"{FEED}/fixture.json", json.dumps(good_bundle()).encode())
    process_landing(store, wh, load_contracts(), datetime.now(UTC))
    vocab.load(wh.conn, vocab.fetch(CACHE))
    install_key(wh.conn, "ci-test-key-0123456789abcdef0123456789abcdef")
    assert run_dbt(DSN, "build", check=False).returncode == 0
    return wh


@pytest.mark.parametrize("name", sorted(cohorts.library()))
def test_every_library_cohort_runs_as_the_reader(tier, tmp_path, name):
    log = cohorts.AuditLog(tmp_path / "audit.jsonl")
    r = cohorts.run(cohorts.library()[name], reader_dsn(), log, "test")
    assert [s["step"] for s in r.attrition] == list(range(len(r.attrition)))
    assert r.source.startswith("DIP-SYNTHEA Eunomia")
    assert all(s["persons"] in (0, "suppressed") or s["persons"] >= 11 for s in r.attrition)
    [entry] = log.entries()
    assert entry["event"] == "ran" and entry["sha256"] == cohorts.library()[name].sha256
    assert cohorts.verify_audit(log.entries()) == []


def test_t2d_hba1c_logic_on_the_fixture_patient(tier):
    """The raw query (as the owner, no suppression): the patient passes every step."""
    sql = cohorts.library()["t2d_hba1c_monitoring"].sql
    assert [r[2] for r in rows(tier, sql)] == [1, 1, 1, 1, 1]


def test_refuses_any_role_but_the_reader(tier, tmp_path):
    log = cohorts.AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(cohorts.Refused, match="not the cohort reader"):
        cohorts.run(cohorts.library()["t2d_hba1c_monitoring"], DSN, log, "test")
    assert log.entries()[0]["event"] == "refused"


def test_refuses_a_tier_that_is_not_synthea(tier, tmp_path):
    """With MIMIC in the warehouse the hook revokes the reader's grants: the run cannot read
    cdm_source and is refused, and audited as such."""
    add_feed(tier, "mimic.fhir_demo")
    try:
        assert run_dbt(DSN, "run", "--select", "shareable", check=False).returncode == 0
        log = cohorts.AuditLog(tmp_path / "audit.jsonl")
        with pytest.raises(cohorts.Refused):
            cohorts.run(cohorts.library()["t2d_hba1c_monitoring"], reader_dsn(), log, "test")
        assert log.entries()[0]["event"] == "refused"
    finally:
        remove_feed(tier, "mimic.fhir_demo")
        assert run_dbt(DSN, "run", "--select", "shareable", check=False).returncode == 0


def _insert_people(cur) -> None:
    """Invented people for the other cohorts, each built to stop at a known step."""
    people = [(9000001 + i, 1960) for i in range(6)]
    cur.executemany("insert into omop_shareable.person (person_id, year_of_birth)"
                    " values (%s, %s)", people)  # fmt: skip
    periods = {
        9000001: ("2015-01-01", "2022-01-01"),
        9000002: ("2015-01-01", "2022-01-01"),
        9000003: ("2010-01-01", "2024-01-01"),
        9000004: ("2010-01-01", "2024-01-01"),
        9000005: ("2015-01-01", "2024-01-01"),
        9000006: ("2015-01-01", "2024-01-01"),
    }
    cur.executemany(
        "insert into omop_shareable.observation_period (observation_period_id, person_id,"
        " observation_period_start_date, observation_period_end_date) values (%s, %s, %s, %s)",
        [(p, p, a, b) for p, (a, b) in periods.items()],
    )
    conditions = [
        (9000001, 320128, "2020-01-10"),  # hypertension, observed 2 years after
        (9000002, 320128, "2021-12-01"),  # hypertension, observed only a month after
        (9000003, 201826, "2015-01-01"),  # T2D, then CKD stage 3 two years later
        (9000003, 443597, "2017-01-01"),
        (9000004, 201826, "2015-01-01"),  # T2D, kidney disease within 30 days: excluded
        (9000004, 443597, "2015-01-10"),
    ]
    cur.executemany(
        "insert into omop_shareable.condition_occurrence (condition_occurrence_id, person_id,"
        " condition_concept_id, condition_start_date) values (%s, %s, %s, %s)",
        [(9100000 + i, p, c, d) for i, (p, c, d) in enumerate(conditions)],
    )
    measurements = [
        (9000001, 3004249, "2020-03-01", 150), (9000001, 3004249, "2020-06-01", 130),
        (9000005, 3038553, "2019-01-01", 33), (9000005, 3038553, "2020-06-01", 31),
        (9000006, 3038553, "2019-01-01", 31),
    ]  # fmt: skip
    cur.executemany(
        "insert into omop_shareable.measurement (measurement_id, person_id,"
        " measurement_concept_id, measurement_date, value_as_number) values (%s, %s, %s, %s, %s)",
        [(9200000 + i, *m) for i, m in enumerate(measurements)],
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # step 0 is the fixture patient plus the six invented people
        ("hypertension_bp_followup", [7, 2, 1, 1, 1]),
        ("t2d_kidney_progression", [7, 3, 1, 1]),
        ("obesity_bmi_followup", [7, 2, 1, 1]),
    ],
)
def test_cohort_logic_on_invented_people(tier, name, expected):
    """Each step's raw count, on people built to stop at a known step (rolled back after)."""
    sql = cohorts.library()[name].sql
    with tier.conn.cursor() as cur:
        try:
            _insert_people(cur)
            cur.execute(sql)
            assert [r[2] for r in sorted(cur.fetchall())] == expected
        finally:
            tier.conn.rollback()


def test_refuses_a_readable_cdm_source_that_is_not_synthea(tier, tmp_path):
    """The reader can read cdm_source, but it says something else: refused."""
    with tier.conn.cursor() as cur:
        cur.execute("update omop_shareable.cdm_source set cdm_source_abbreviation = 'DIP-MIMIC'")
    tier.conn.commit()
    try:
        log = cohorts.AuditLog(tmp_path / "audit.jsonl")
        with pytest.raises(cohorts.Refused, match="not DIP-SYNTHEA"):
            cohorts.run(cohorts.library()["t2d_hba1c_monitoring"], reader_dsn(), log, "test")
        assert log.entries()[0]["event"] == "refused"
    finally:
        with tier.conn.cursor() as cur:
            cur.execute("update omop_shareable.cdm_source"
                        " set cdm_source_abbreviation = 'DIP-SYNTHEA'")  # fmt: skip
        tier.conn.commit()


def test_a_failed_run_is_audited(tier, tmp_path):
    log = cohorts.AuditLog(tmp_path / "audit.jsonl")
    bad = cohorts.Cohort("broken", 1, "t", "q", (), "select 0, 'x', count(*) from nowhere",
                         "0" * 64, "x")  # fmt: skip
    with pytest.raises(Exception, match="nowhere"):
        cohorts.run(bad, reader_dsn(), log, "test")
    assert log.entries()[0]["event"] == "failed"
