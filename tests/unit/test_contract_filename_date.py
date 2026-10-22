"""source.filename_date: the business date in a file name, checked at intake."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from dip.contracts import ContractError, load_contract, load_contracts

RECEIVED = date(2026, 10, 21)


@pytest.fixture(scope="module")
def contract():
    return load_contracts()["healthcare.eligibility"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("elig_20261021.csv", date(2026, 10, 21)),
        ("elig_20261014.csv", date(2026, 10, 14)),  # 7 days before: the limit
        ("elig_good.csv", None),  # no date: the received date is used
    ],
)
def test_good_names(contract, name, expected):
    assert contract.filename_date(name, RECEIVED) == (expected, None)


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("elig_20261399.csv", "not a real"),
        ("elig_20260230.csv", "not a real"),
        ("elig_2026102.csv", "not a real"),  # strptime would read it as 2026-10-02
        ("elig_202610210.csv", "not a real"),
        ("elig_20261022.csv", "after the received date"),
        ("elig_20261013.csv", "8 days before"),
    ],
)
def test_bad_names(contract, name, problem):
    day, why = contract.filename_date(name, RECEIVED)
    assert day is None
    assert problem in why


def _with_rule(tmp_path, rule):
    spec = yaml.safe_load(load_contracts()["healthcare.eligibility"].path.read_text("utf-8"))
    spec["source"]["filename_date"] = rule
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"regex": "elig_([0-9]+", "format": "%Y%m%d"}, "regex"),
        ({"regex": "elig_[0-9]+", "format": "%Y%m%d"}, "exactly one group"),
        ({"regex": "elig_([0-9]+)"}, "format"),
    ],
)
def test_contract_mistakes_fail_at_load(tmp_path, rule, message):
    with pytest.raises(ContractError, match=message):
        load_contract(_with_rule(tmp_path, rule))
