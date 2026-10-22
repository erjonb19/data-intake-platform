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
    with pytest.raises(cohorts.Refused, match="dip_cohort_reader"):
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
