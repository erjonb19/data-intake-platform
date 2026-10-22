"""Stage 1: eligibility CSV files through the intake into Postgres bronze and ops."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING
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
    assert rows(
        warehouse,
        "select level, reason_code, record_ref from ops.quarantine where file_sha256 = %s",
        (by_name["elig_bad_rows.csv"].file_sha256,),
    ) == [("record", "VALUE.NOT_ALLOWED:plan_code", "row=7")]

    # Reruns are idempotent: the same bytes are recognized and skipped.
    store.put(
        BUCKET_LANDING, f"{FEED}/elig_again.csv", (FIXTURES / "good/elig_good.csv").read_bytes()
    )
    [again] = process_landing(store, warehouse, load_contracts(), NOW)
    assert again.status == "duplicate"
    assert scalar(warehouse, "select count(*) from bronze.csv_record") == 25
