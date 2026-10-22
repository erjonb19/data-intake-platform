"""Stage 1: eligibility CSV files through the intake into Postgres bronze and ops."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING, BUCKET_QUARANTINE
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import rows, scalar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_csv_rows_land_in_bronze_and_rejections_in_ops(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    for name in ("good/elig_good.csv", "bad/elig_missing_member_id.csv", "bad/elig_bad_rows.csv"):
        store.put(BUCKET_LANDING, f"{FEED}/{Path(name).name}", (FIXTURES / name).read_bytes())
    outcomes = process_landing(store, warehouse, load_contracts(), NOW)
    by_name = {o.source_key.split("/")[-1]: o for o in outcomes}

    assert rows(
        warehouse,
        "select status, accepted_count, rejected_count from ops.intake_file order by 1, 2",
    ) == [("loaded", 6, 0), ("loaded_with_rejections", 19, 1), ("quarantined", 0, 0)]
    assert scalar(warehouse, "select count(*) from bronze.csv_record") == 25
    # Values are stored exactly as received, as text.
    assert rows(
        warehouse,
        "select record->>'member_id', record->>'coverage_end' from bronze.csv_record"
        " where file_sha256 = %s and row_number = 1",
        (by_name["elig_good.csv"].file_sha256,),
    ) == [("M000000001", "")]
    # The schema-drifted file: one file-level quarantine row, nothing in bronze.
    bad = by_name["elig_missing_member_id.csv"].file_sha256
    assert rows(
        warehouse, "select level, reason_code from ops.quarantine where file_sha256 = %s", (bad,)
    ) == [("file", "SCHEMA.MISSING_COLUMN:member_id")]
    assert (
        scalar(warehouse, "select count(*) from bronze.csv_record where file_sha256 = %s", (bad,))
        == 0
    )
    # ...and the file itself plus a reason document are in the quarantine bucket.
    [qkey] = by_name["elig_missing_member_id.csv"].quarantine_keys
    assert (
        store.get(BUCKET_QUARANTINE, qkey)
        == (FIXTURES / "bad/elig_missing_member_id.csv").read_bytes()
    )
    reason = json.loads(store.get(BUCKET_QUARANTINE, qkey.removesuffix(".csv") + ".reason.json"))
    assert [v["reason_code"] for v in reason["violations"]] == ["SCHEMA.MISSING_COLUMN:member_id"]
    assert rows(
        warehouse,
        "select level, reason_code, record_ref from ops.quarantine where file_sha256 = %s",
        (by_name["elig_bad_rows.csv"].file_sha256,),
    ) == [("record", "VALUE.NOT_ALLOWED:plan_code", "row=7")]

    # Reruns are idempotent: the same bytes are recognized and not loaded again, under the
    # same name (a duplicate) or another one (a new delivery of the same content).
    good = (FIXTURES / "good/elig_good.csv").read_bytes()
    store.put(BUCKET_LANDING, f"{FEED}/elig_good.csv", good)
    [again] = process_landing(store, warehouse, load_contracts(), NOW)
    assert again.status == "duplicate"
    store.put(BUCKET_LANDING, f"{FEED}/elig_again.csv", good)
    [again] = process_landing(store, warehouse, load_contracts(), NOW)
    assert again.status == "redelivered"
    assert scalar(warehouse, "select count(*) from bronze.csv_record") == 25
