"""Intake against a real Postgres. Skipped unless DIP_TEST_DSN is set.

DIP_TEST_DSN must point at a throwaway database: these tests drop and recreate the bronze
and ops schemas. With DIP_SYNTHEA_DIR set, every Synthea bundle there also goes through
the intake, and none may be rejected at file level (the contract must fit real exports).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from dip.warehouse import Warehouse
from tests.fixtures.fhir.make_fixtures import bad_missing_birthdate, good_bundle

DSN = os.environ.get("DIP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="DIP_TEST_DSN not set")
FEED = "healthcare.fhir_patient_bundle"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def warehouse():
    import psycopg

    conn = psycopg.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("drop schema if exists bronze cascade; drop schema if exists ops cascade")
    conn.commit()
    wh = Warehouse(conn)
    wh.init()
    wh.init()  # idempotent
    yield wh
    conn.close()


def scalar(wh, sql):
    with wh.conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_fixture_files_end_to_end(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, f"{FEED}/good.json", json.dumps(good_bundle()).encode())
    store.put(BUCKET_LANDING, f"{FEED}/bad.json", json.dumps(bad_missing_birthdate()).encode())
    process_landing(store, warehouse, load_contracts(), NOW)

    assert scalar(warehouse, "select count(*) from bronze.fhir_resource") == 9
    assert scalar(warehouse, "select count(distinct patient_id) from bronze.fhir_resource") == 1
    assert (
        scalar(warehouse, "select reason_code from ops.quarantine where level = 'file'")
        == "FHIR.PATIENT_INVALID:FHIR.MISSING_FIELD:Patient.birthDate"
    )
    assert scalar(
        warehouse, "select string_agg(status, ',' order by status) from ops.intake_file"
    ) == ("loaded,quarantined")


def test_failed_write_leaves_nothing_behind(warehouse, tmp_path, monkeypatch):
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, f"{FEED}/good.json", json.dumps(good_bundle()).encode())
    real = warehouse.record_outcome

    def broken(intake, *a, **kw):
        return real(intake.__class__(**{**intake.__dict__, "status": "not-a-status"}), *a, **kw)

    monkeypatch.setattr(warehouse, "record_outcome", broken)
    with pytest.raises(Exception, match="check"):
        process_landing(store, warehouse, load_contracts(), NOW)
    assert scalar(warehouse, "select count(*) from bronze.fhir_resource") == 0
    # The landing file stays put so the next run retries it.
    assert store.list(BUCKET_LANDING) == [f"{FEED}/good.json"]


@pytest.mark.skipif(not os.environ.get("DIP_SYNTHEA_DIR"), reason="DIP_SYNTHEA_DIR not set")
def test_real_synthea_export(warehouse, tmp_path):
    files = sorted(Path(os.environ["DIP_SYNTHEA_DIR"]).glob("*.json"))
    assert files, "no Synthea bundles found"
    store = LocalStore(tmp_path)
    for f in files:
        store.put(BUCKET_LANDING, f"{FEED}/{f.name}", f.read_bytes())
    outcomes = process_landing(store, warehouse, load_contracts(), datetime.now(UTC))
    rejected = [(o.source_key, o.reason_codes) for o in outcomes if o.status == "quarantined"]
    assert not rejected, rejected
    with warehouse.conn.cursor() as cur:
        cur.execute(
            "select reason_code, min(detail), count(*) from ops.quarantine"
            " group by 1 order by 3 desc limit 10"
        )
        print("record-level quarantine:", cur.fetchall())
    assert scalar(warehouse, "select count(distinct patient_id) from bronze.fhir_resource") == len(
        files
    )
