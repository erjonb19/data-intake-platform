"""The small cron reader behind FEED.LATE (S2.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dip.sla import Cron, parse_grace


def at(*a):
    return datetime(*a, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expr", "before", "expected"),
    [
        ("0 6 * * *", at(2026, 10, 22, 7, 30), at(2026, 10, 22, 6, 0)),
        ("0 6 * * *", at(2026, 10, 22, 5, 59), at(2026, 10, 21, 6, 0)),
        ("0 6 * * *", at(2026, 10, 22, 6, 0), at(2026, 10, 22, 6, 0)),
        ("0 18 * * 1-5", at(2026, 10, 25, 12, 0), at(2026, 10, 23, 18, 0)),  # Sun -> Fri
        ("*/15 * * * *", at(2026, 10, 22, 7, 44), at(2026, 10, 22, 7, 30)),
        ("30 2 1 * *", at(2026, 10, 22, 0, 0), at(2026, 10, 1, 2, 30)),
        ("0 0 * * 7", at(2026, 10, 22, 0, 0), at(2026, 10, 18, 0, 0)),  # 7 is Sunday
        ("0 9 15 * 1", at(2026, 10, 22, 0, 0), at(2026, 10, 19, 9, 0)),  # dom or dow: Mon 19th
    ],
)
def test_previous(expr, before, expected):
    assert Cron.parse(expr).previous(before) == expected


@pytest.mark.parametrize(
    "expr", ["0 6 * *", "61 * * * *", "* 24 * * *", "0 6 * * 8", "*/0 * * * *"]
)
def test_bad_cron(expr):
    with pytest.raises(ValueError):
        Cron.parse(expr)


@pytest.mark.parametrize(
    ("text", "td"),
    [("2h", timedelta(hours=2)), ("90m", timedelta(minutes=90)), ("1d", timedelta(days=1))],
)
def test_grace(text, td):
    assert parse_grace(text) == td


def test_bad_grace():
    with pytest.raises(ValueError):
        parse_grace("2 hours")


def test_contracts_with_a_bad_rule_do_not_load(tmp_path):
    from dip.contracts import ContractError, load_contracts

    src = (load_contracts()["healthcare.eligibility"].path).read_text(encoding="utf-8")
    (tmp_path / "x.yaml").write_text(src.replace('cron: "0 6 * * *"', 'cron: "0 25 * * *"'),
                                     encoding="utf-8")  # fmt: skip
    with pytest.raises(ContractError, match="expected_arrival"):
        load_contracts((tmp_path,))
