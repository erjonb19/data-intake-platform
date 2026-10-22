"""Stage 1: eligibility CSV -> bronze -> silver -> gold with dbt, against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"
DAY1 = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
DAY2 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)


def build():
    result = run_dbt(DSN, "build", "--select", f"tag:{FEED}", check=False)
    assert result.returncode == 0, "dbt build of the eligibility models failed; see output"


def test_eligibility_reaches_gold_and_the_latest_file_wins(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    for name in ("good/elig_good.csv", "bad/elig_missing_member_id.csv", "bad/elig_bad_rows.csv"):
        store.put(BUCKET_LANDING, f"{FEED}/{Path(name).name}", (FIXTURES / name).read_bytes())
    process_landing(store, warehouse, load_contracts(), DAY1)
    build()

    assert scalar(warehouse, "select count(*) from silver.eligibility_record") == 25
    assert scalar(warehouse, "select count(*) from gold.member_eligibility") == 25
    assert rows(
        warehouse,
        "select member_id, dob, plan_code, coverage_start, coverage_end, patient_ref"
        " from gold.member_eligibility where member_id in ('M000000002', 'M000000004')"
        " order by 1",
    ) == [
        ("M000000002", _d("1975-07-30"), "PPO", _d("2023-06-01"), _d("2026-06-30"),
         "fixture-0002"),
        ("M000000004", _d("1990-03-21"), "HDHP", _d("2022-09-15"), None, None),
    ]  # fmt: skip
    # Nothing from the quarantined row reached silver or gold.
    assert (
        scalar(warehouse, "select count(*) from gold.member_eligibility where plan_code = 'GOLD'")
        == 0
    )

    # Day 2: member 2 changes plan. Gold follows the newest file; silver keeps both.
    day2 = (FIXTURES / "good/elig_good.csv").read_text(encoding="utf-8")
    day2 = day2.replace(
        "M000000002,Sam,Sample,1975-07-30,M,PPO", "M000000002,Sam,Sample,1975-07-30,M,EPO"
    )
    store.put(BUCKET_LANDING, f"{FEED}/elig_20261022.csv", day2.encode("utf-8"))
    process_landing(store, warehouse, load_contracts(), DAY2)
    build()
    assert rows(
        warehouse,
        "select plan_code, as_of from gold.member_eligibility where member_id = 'M000000002'",
    ) == [("EPO", DAY2)]
    assert scalar(warehouse, "select count(*) from silver.eligibility_record") == 31
    assert scalar(warehouse, "select count(*) from gold.member_eligibility") == 25


def _d(text: str):
    from datetime import date

    return date.fromisoformat(text)
