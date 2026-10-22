"""CSV contract rules beyond the eligibility feed: typed columns, quoting, whitespace,
duplicates, and contract mistakes caught at load time (review findings on Stage 1a)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from dip import BUCKET_LANDING, BUCKET_QUARANTINE
from dip.contracts import Contract, ContractError, load_contract
from dip.csvfeed import validate_csv
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.unit.test_intake import FakeWarehouse

NOW = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)
BASE = {
    "feed": "test.typed",
    "version": 1,
    "owner": "tests@example.com",
    "source": {"channels": ["minio"], "filename_pattern": "typed_*.csv"},
    "format": {"type": "csv"},
    "on_row_violation": "quarantine_row",
    "max_bad_row_ratio": 1,
    "columns": [
        {"name": "id", "type": "integer", "required": True, "unique": True, "min": 1, "max": 1000},
        {"name": "amount", "type": "decimal", "min": "0.00", "max": 99.5},
        {"name": "flag", "type": "boolean", "allowed": [True]},
        {"name": "code", "type": "string", "unique": True},
        {"name": "note", "type": "string"},
    ],
}


def contract(tmp_path: Path, **overrides) -> Contract:
    path = tmp_path / "typed.yaml"
    path.write_text(yaml.safe_dump({**BASE, **overrides}), encoding="utf-8")
    return load_contract(path)


def run(c, text: str):
    return validate_csv(text.encode("utf-8"), c, NOW)


def outcomes(result):
    return [(r.row_number, v.reason_code) for r, v in result.rejected]


def test_typed_columns(tmp_path):
    c = contract(tmp_path)
    r = run(
        c,
        "id,amount,flag,code,note\n"
        "1,10.25,true,a,\n"  # ok
        "2,abc,true,b,\n"  # decimal
        "x,1,true,c,\n"  # integer
        "4,1,yes,d,\n"  # boolean
        "5,1,false,e,\n"  # boolean not allowed
        "6,99.6,true,f,\n"  # decimal above max
        "0,1,TRUE,g,\n"  # integer below min (TRUE is a valid boolean)
        "8,-0.01,true,h,\n",  # decimal below min
    )
    assert [row.row_number for row in r.accepted] == [1]
    assert outcomes(r) == [
        (2, "VALUE.TYPE:amount"),
        (3, "VALUE.TYPE:id"),
        (4, "VALUE.TYPE:flag"),
        (5, "VALUE.NOT_ALLOWED:flag"),
        (6, "VALUE.OUT_OF_RANGE:amount"),
        (7, "VALUE.OUT_OF_RANGE:id"),
        (8, "VALUE.OUT_OF_RANGE:amount"),
    ]


def test_quoted_fields_crlf_and_whitespace_are_trimmed(tmp_path):
    c = contract(tmp_path)
    r = run(c, 'id,amount,flag,code,note\r\n 1 ,1, true ,"a, b","line one\r\nline two"\r\n')
    [row] = r.accepted
    # Stored trimmed, otherwise as received: commas and newlines inside quotes survive.
    assert row.record == {
        "id": "1",
        "amount": "1",
        "flag": "true",
        "code": "a, b",
        "note": "line one\r\nline two",
    }


def test_delimiter_only_row_is_checked_not_dropped(tmp_path):
    c = contract(tmp_path)
    r = run(c, "id,amount,flag,code,note\n1,1,true,a,\n,,,,\n\n2,1,true,b,\n")
    assert outcomes(r) == [(2, "VALUE.REQUIRED:id")]
    assert [row.row_number for row in r.accepted] == [1, 3]
    assert r.skipped["blank_line"] == 1


def test_unique_ignores_empty_values(tmp_path):
    c = contract(tmp_path)
    r = run(c, "id,amount,flag,code,note\n1,1,true,,\n2,1,true,,\n")
    assert not r.rejected and len(r.accepted) == 2


def test_duplicate_key_is_ambiguous_even_when_the_other_copy_fails(tmp_path):
    c = contract(tmp_path)
    r = run(c, "id,amount,flag,code,note\n1,1,true,a,\n1,abc,true,b,\n")
    # Row 2 keeps its own reason; row 1 is a duplicate of it, so it does not load either.
    assert outcomes(r) == [(1, "KEY.DUPLICATE:id"), (2, "VALUE.TYPE:amount")]
    assert r.accepted == []


def test_whole_file_rejection_keeps_each_row_reason(tmp_path):
    c = contract(tmp_path, max_bad_row_ratio=0.1)
    store = LocalStore(tmp_path / "store")
    data = "id,amount,flag,code,note\n1,1,true,a,\n2,abc,true,b,\n"
    store.put(BUCKET_LANDING, "test.typed/typed_1.csv", data.encode())
    [o] = process_landing(store, FakeWarehouse(), {c.feed: c}, NOW)
    assert o.status == "quarantined"
    assert o.reason_codes == ("FILE.BAD_ROW_RATIO", "VALUE.TYPE:amount")
    [reason_key] = [k for k in store.list(BUCKET_QUARANTINE) if k.endswith(".reason.json")]
    doc = json.loads(store.get(BUCKET_QUARANTINE, reason_key))
    assert [v["reason_code"] for v in doc["violations"]] == ["FILE.BAD_ROW_RATIO"]
    assert doc["rows"] == [{"row_number": 2, "reason_code": "VALUE.TYPE:amount"}]


def test_default_ratio_of_zero_rejects_any_bad_row(tmp_path):
    spec = {k: v for k, v in BASE.items() if k != "max_bad_row_ratio"}
    path = tmp_path / "strict.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    r = run(load_contract(path), "id,amount,flag,code,note\n1,1,true,a,\n2,abc,true,b,\n")
    assert [v.reason_code for v in r.file_violations] == ["FILE.BAD_ROW_RATIO"]


def test_today_bound_uses_the_utc_received_date(tmp_path):
    cols = [{"name": "d", "type": "date", "max": "today"}]
    c = contract(tmp_path, columns=cols)
    # 23:30 UTC on Oct 21 is still Oct 21; a date of Oct 22 is in the future.
    assert not run(c, "d\n2026-10-21\n").rejected
    assert outcomes(run(c, "d\n2026-10-22\n")) == [(1, "VALUE.OUT_OF_RANGE:d")]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"format": {"type": "csv", "encoding": "utf-9"}}, "unknown encoding"),
        ({"columns": [{"name": "a", "pattern": "("}]}, "columns.a.pattern"),
        ({"columns": [{"name": "a", "type": "integer", "min": "zero"}]}, "columns.a.min"),
        ({"columns": [{"name": "a", "type": "date", "max": "tomorrow"}]}, "columns.a.max"),
        ({"columns": [{"name": "a", "type": "string", "min": 3}]}, "columns.a.min"),
        ({"columns": [{"name": "a", "type": "date", "allowed": ["soon"]}]}, "columns.a.allowed"),
    ],
)
def test_contract_mistakes_fail_at_load(tmp_path, override, message):
    with pytest.raises(ContractError, match=message):
        contract(tmp_path, **override)


@pytest.mark.parametrize(
    "override",
    [
        {"columns": None},  # csv requires columns
        {"columns": [{"name": "Bad Name"}]},
        {"columns": [{"name": "a", "type": "float"}]},
        {"format": {"type": "csv", "header": False}},
    ],
)
def test_schema_rejects_bad_csv_contracts(tmp_path, override):
    spec = {**BASE, **override}
    if spec["columns"] is None:
        del spec["columns"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(ContractError):
        load_contract(path)
