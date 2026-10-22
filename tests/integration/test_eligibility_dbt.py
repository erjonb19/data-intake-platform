"""Stage 1: eligibility CSV -> bronze -> silver -> gold with dbt, against a real Postgres."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"
HEADER = "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref"


def build():
    result = run_dbt(DSN, "build", "--select", f"tag:{FEED}", check=False)
    assert result.returncode == 0, "dbt build of the eligibility models failed; see output"


def land(store, warehouse, name: str, data: bytes, at: datetime):
    store.put(BUCKET_LANDING, f"{FEED}/{name}", data)
    return process_landing(store, warehouse, load_contracts(), at)


def member(member_id: str, plan: str, name: str = "Pat") -> str:
    return f"{member_id},{name},Invented,1980-01-01,F,{plan},2024-01-01,,"


def gold(warehouse, member_id: str):
    return rows(
        warehouse,
        "select plan_code, file_date, in_latest_file from gold.member_eligibility"
        " where member_id = %s",
        (member_id,),
    )


def test_fixtures_reach_gold(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    at = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    for name in ("good/elig_good.csv", "bad/elig_missing_member_id.csv", "bad/elig_bad_rows.csv"):
        land(store, warehouse, Path(name).name, (FIXTURES / name).read_bytes(), at)
    build()

    assert scalar(warehouse, "select count(*) from silver.eligibility_record") == 25
    assert scalar(warehouse, "select count(*) from gold.member_eligibility") == 25
    assert rows(
        warehouse,
        "select member_id, dob, plan_code, coverage_start, coverage_end, patient_ref"
        " from gold.member_eligibility where member_id in ('M000000002', 'M000000004')"
        " order by 1",
    ) == [
        ("M000000002", date(1975, 7, 30), "PPO", date(2023, 6, 1), date(2026, 6, 30),
         "fixture-0002"),
        ("M000000004", date(1990, 3, 21), "HDHP", date(2022, 9, 15), None, None),
    ]  # fmt: skip
    # No date in these names: file_date falls back to the UTC date received.
    assert rows(warehouse, "select distinct file_date from silver.eligibility_record") == [
        (date(2026, 10, 21),)
    ]
    # Nothing from the quarantined row reached silver or gold.
    assert (
        scalar(warehouse, "select count(*) from silver.eligibility_record where plan_code = 'GOLD'")
        == 0
    )


def test_full_file_semantics(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    day1 = "\n".join([HEADER, member("M200000001", "HMO"), member("M200000002", "PPO"),
                      member("M200000003", "EPO")]) + "\n"  # fmt: skip
    land(
        store, warehouse, "elig_20261020.csv", day1.encode(), datetime(2026, 10, 20, 6, tzinfo=UTC)
    )
    # Day 2: member 1 changes plan, member 2 is gone (disenrolled), member 3's row is bad.
    day2 = "\n".join([HEADER, member("M200000001", "EPO"), member("M200000003", "GOLD")]) + "\n"
    # 20 more members keep the one bad row under the contract's 5% bad-row limit.
    day2 += "\n".join(member(f"M2000001{i:02d}", "HMO") for i in range(20)) + "\n"
    land(
        store, warehouse, "elig_20261021.csv", day2.encode(), datetime(2026, 10, 21, 6, tzinfo=UTC)
    )
    build()

    d1, d2 = date(2026, 10, 20), date(2026, 10, 21)
    assert gold(warehouse, "M200000001") == [("EPO", d2, True)]
    assert gold(warehouse, "M200000002") == [("PPO", d1, False)]  # disenrolled: not current
    assert gold(warehouse, "M200000003") == [("EPO", d1, False)]  # newest row quarantined
    assert (
        scalar(warehouse, "select count(*) from gold.member_eligibility where in_latest_file") == 21
    )

    # A late copy of an older file (received after day 2, dated before it) changes nothing.
    late = "\n".join([HEADER, member("M200000001", "HDHP", name="Late")]) + "\n"
    land(
        store, warehouse, "elig_20261019.csv", late.encode(), datetime(2026, 10, 22, 6, tzinfo=UTC)
    )
    build()
    assert gold(warehouse, "M200000001") == [("EPO", d2, True)]
    assert (
        scalar(warehouse, "select count(*) from silver.eligibility_record") == 3 + 21 + 1
    )  # day 2: 22 rows, 1 quarantined


def test_identical_file_resent_on_a_later_day_is_the_latest(warehouse, tmp_path):
    """A full-file feed resends yesterday's exact content as today's file: it is not loaded
    again, but it is a new delivery, so its business date makes it the latest file."""
    store = LocalStore(tmp_path)
    both = ("\n".join([HEADER, member("M300000001", "HMO"), member("M300000002", "PPO")])
            + "\n").encode()  # fmt: skip
    one = ("\n".join([HEADER, member("M300000001", "EPO")]) + "\n").encode()
    land(store, warehouse, "elig_20261019.csv", both, datetime(2026, 10, 19, 6, tzinfo=UTC))
    land(store, warehouse, "elig_20261020.csv", one, datetime(2026, 10, 20, 6, tzinfo=UTC))
    [again] = land(
        store, warehouse, "elig_20261021.csv", both, datetime(2026, 10, 21, 6, tzinfo=UTC)
    )
    assert again.status == "redelivered"
    # Re-landing the very same name and content is still only a duplicate.
    [dup] = land(store, warehouse, "elig_20261021.csv", both, datetime(2026, 10, 21, 7, tzinfo=UTC))
    assert dup.status == "duplicate"
    build()

    d3 = date(2026, 10, 21)
    assert gold(warehouse, "M300000001") == [("HMO", d3, True)]
    assert gold(warehouse, "M300000002") == [("PPO", d3, True)]
    assert scalar(warehouse, "select count(*) from ops.intake_file") == 2
    assert scalar(warehouse, "select count(*) from ops.intake_delivery") == 3
    assert scalar(warehouse, "select count(*) from silver.eligibility_record") == 3


def test_file_name_dates_are_validated(warehouse, tmp_path):
    """FILE.NAME_DATE: not a real date, after the received date, or more than 7 days before
    it. Nothing of such a file reaches silver; a name with no date is accepted."""
    store = LocalStore(tmp_path)
    at = datetime(2026, 10, 21, 6, tzinfo=UTC)
    body = ("\n".join([HEADER, member("M400000001", "HMO")]) + "\n").encode()
    bad = ["elig_20261399.csv", "elig_2026102.csv", "elig_20261022.csv", "elig_20261013.csv"]
    for name in bad:
        [o] = land(store, warehouse, name, body, at)
        assert (o.status, o.reason_codes) == ("quarantined", ("FILE.NAME_DATE",)), name
    # The same content under a good name loads; resent under a bad name, it is quarantined
    # again and not recorded as a delivery.
    [ok] = land(store, warehouse, "elig_20261014.csv", body, at)
    assert ok.status == "loaded"
    [o] = land(store, warehouse, "elig_20261023.csv", body, at)
    assert (o.status, o.reason_codes) == ("quarantined", ("FILE.NAME_DATE",))
    other = ("\n".join([HEADER, member("M400000002", "PPO")]) + "\n").encode()
    [undated] = land(store, warehouse, "elig_monthly.csv", other, at)
    assert undated.status == "loaded"
    build()

    assert rows(
        warehouse, "select member_id, file_date from silver.eligibility_record order by 1"
    ) == [("M400000001", date(2026, 10, 14)), ("M400000002", date(2026, 10, 21))]
    assert (
        scalar(
            warehouse, "select count(*) from ops.quarantine where reason_code = 'FILE.NAME_DATE'"
        )
        == len(bad) + 1
    )
