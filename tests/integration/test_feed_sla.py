"""FEED.LATE incidents (S2.6) against Postgres, with a fixed clock: the eligibility file is
due daily at 06:00 UTC with 2 hours' grace."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.sla import check
from dip.storage import LocalStore
from tests.integration.conftest import rows

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
ELIG = {"healthcare.eligibility": load_contracts()["healthcare.eligibility"]}


def at(*a):
    return datetime(*a, tzinfo=UTC)


def incidents(wh):
    return rows(wh, "select feed, window_start, closed_at is not null from ops.incident"
                    " order by incident_id")  # fmt: skip


def test_late_once_then_closed_when_the_file_arrives(warehouse, tmp_path):
    # Before today's deadline (06:00 + 2h) only yesterday's window is judged: an empty
    # warehouse missed it too.
    assert check(warehouse.conn, ELIG, at(2026, 10, 22, 7, 59))["opened"] == [
        "healthcare.eligibility 2026-10-21 06:00"
    ]
    # After it, one incident for today's 06:00 window; checking again does not open another.
    assert check(warehouse.conn, ELIG, at(2026, 10, 22, 8, 1))["opened"] == [
        "healthcare.eligibility 2026-10-22 06:00"
    ]
    assert check(warehouse.conn, ELIG, at(2026, 10, 22, 8, 16))["opened"] == []
    # The file arrives late (quarantined for its content: still an arrival); it closes.
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, "healthcare.eligibility/elig_missing_member_id.csv",
              (FIXTURES / "bad/elig_missing_member_id.csv").read_bytes())  # fmt: skip
    process_landing(store, warehouse, load_contracts(), at(2026, 10, 22, 9, 0))
    # A daily full file: one arrival closes both open windows.
    assert check(warehouse.conn, ELIG, at(2026, 10, 22, 9, 15))["closed"] == [
        "healthcare.eligibility 2026-10-21 06:00",
        "healthcare.eligibility 2026-10-22 06:00",
    ]
    assert incidents(warehouse) == [
        ("healthcare.eligibility", at(2026, 10, 21, 6, 0), True),
        ("healthcare.eligibility", at(2026, 10, 22, 6, 0), True),
    ]


def test_a_file_in_time_is_not_late(warehouse, tmp_path):
    """Yesterday's window is open (an empty warehouse); today's file arrives in time."""
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, "healthcare.eligibility/elig_good.csv",
              (FIXTURES / "good/elig_good.csv").read_bytes())  # fmt: skip
    process_landing(store, warehouse, load_contracts(), at(2026, 10, 22, 6, 30))
    assert check(warehouse.conn, ELIG, at(2026, 10, 22, 9, 0))["opened"] == []
