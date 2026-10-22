"""Excel contracts (Stage 2): the eligibility contract read from an .xlsx workbook gives the
same results and reason codes as the CSV path. Workbooks are built here with openpyxl from
invented values, so no binary fixture is committed."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime

import openpyxl
import pytest

from dip.contracts import Contract, load_contracts
from dip.csvfeed import validate_csv
from dip.excelfeed import cell_text, validate_excel

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
HEADER = ["member_id", "first_name", "last_name", "dob", "sex", "plan_code", "coverage_start",
          "coverage_end", "patient_ref"]  # fmt: skip
GOOD = ["M000000001", "Alex", "Example", date(1980, 2, 14), "F", "HMO", date(2024, 1, 1), None,
        "ref-1"]  # fmt: skip


@pytest.fixture(scope="module")
def contract() -> Contract:
    """The eligibility contract, read as Excel from a sheet named Members."""
    c = load_contracts()["healthcare.eligibility"]
    fmt = {"type": "excel", "sheet": "Members", "header_row": 1}
    return Contract(c.feed, c.version, {**c.spec, "format": fmt}, c.path)


def workbook(*rows, sheet="Members", title_rows=()) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in (*title_rows, *rows):
        ws.append(list(r))
    wb.create_sheet("Notes").append(["not read"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def codes(result):
    return sorted(v.reason_code for v in result.file_violations)


def with_format(contract, **fmt):
    spec = {**contract.spec, "format": {**contract.spec["format"], **fmt}}
    return Contract(contract.feed, contract.version, spec, contract.path)


def test_good_workbook_loads_like_the_csv(contract):
    rows = [GOOD, ["M000000002", "Sam", "Sample", date(1975, 6, 1), "M", "PPO",
                   date(2023, 3, 1), date(2025, 12, 31), "ref-2"]]  # fmt: skip
    r = validate_excel(workbook(HEADER, *rows), contract, NOW)
    assert not r.file_violations and not r.rejected
    assert [row.record for row in r.accepted] == [
        dict(zip(HEADER, ["M000000001", "Alex", "Example", "1980-02-14", "F", "HMO",
                          "2024-01-01", "", "ref-1"], strict=True)),
        dict(zip(HEADER, ["M000000002", "Sam", "Sample", "1975-06-01", "M", "PPO",
                          "2023-03-01", "2025-12-31", "ref-2"], strict=True)),
    ]  # fmt: skip
    csv_text = "\n".join(",".join(cell_text(v) for v in row) for row in (HEADER, *rows))
    eligibility_csv = load_contracts()["healthcare.eligibility"]
    same = validate_csv((csv_text + "\n").encode(), eligibility_csv, NOW)
    assert [x.record for x in same.accepted] == [x.record for x in r.accepted]


def test_title_rows_above_the_header_and_trailing_blank_rows(contract):
    data = workbook(HEADER, GOOD, [None] * 9, [None] * 9, title_rows=[["Eligibility"], []])
    r = validate_excel(data, with_format(contract, header_row=3), NOW)
    assert not r.file_violations
    assert len(r.accepted) == 1
    assert r.skipped["blank_line"] == 2


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (12.0, "12"),
        (12.5, "12.5"),
        (7, "7"),
        (True, "true"),
        (datetime(2026, 10, 22), "2026-10-22"),
        (datetime(2026, 10, 22, 8, 30), "2026-10-22T08:30:00"),
        (date(2026, 1, 2), "2026-01-02"),
        ("  padded ", "padded"),
    ],
)
def test_cell_text(value, text):
    assert cell_text(value) == text


def test_schema_drift_gives_the_csv_reason_codes(contract):
    header = [h for h in HEADER if h != "member_id"]
    r = validate_excel(workbook(header, GOOD[1:]), contract, NOW)
    assert codes(r) == ["SCHEMA.MISSING_COLUMN:member_id"]
    assert not r.accepted


def test_row_level_violation_quarantines_the_row(contract):
    bad = list(GOOD)
    bad[0], bad[5] = "M000000009", "GOLD"
    r = validate_excel(workbook(HEADER, *([GOOD] * 0), GOOD, bad), contract, NOW)
    assert codes(r) == ["FILE.BAD_ROW_RATIO"]  # 1 of 2 is above the 5% ratio
    assert [(row.row_number, v.reason_code) for row, v in r.rejected] == [
        (2, "VALUE.NOT_ALLOWED:plan_code")
    ]


def test_a_value_beyond_the_header_is_a_field_count_violation(contract):
    r = validate_excel(workbook(HEADER, [*GOOD, "stray"]), contract, NOW)
    assert [v.reason_code for _, v in r.rejected] == ["ROW.FIELD_COUNT"]


def test_missing_sheet(contract):
    r = validate_excel(workbook(HEADER, GOOD, sheet="Sheet1"), contract, NOW)
    assert codes(r) == ["SCHEMA.MISSING_SHEET:Members"]


def test_first_sheet_when_the_contract_names_none(contract):
    fmt = {"type": "excel"}
    c = Contract(contract.feed, contract.version, {**contract.spec, "format": fmt}, contract.path)
    r = validate_excel(workbook(HEADER, GOOD, sheet="Whatever"), c, NOW)
    assert not r.file_violations and len(r.accepted) == 1


@pytest.mark.parametrize("data", [b"", b"member_id,first_name\n", b"PK\x03\x04 truncated"])
def test_not_a_workbook(contract, data):
    assert codes(validate_excel(data, contract, NOW)) == ["FILE.INVALID_XLSX"]


def test_macros_are_refused(contract):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook(HEADER, GOOD))) as src, zipfile.ZipFile(
        buf, "w"
    ) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/vbaProject.bin", b"\x00fake")
    assert codes(validate_excel(buf.getvalue(), contract, NOW)) == ["FILE.MACROS"]


def test_formula_without_a_cached_value_reads_empty(contract):
    # openpyxl writes formulas without calculating them; data_only reads None for them.
    row = list(GOOD)
    row[1] = '=UPPER("alex")'
    r = validate_excel(workbook(HEADER, row), contract, NOW)
    assert [v.reason_code for _, v in r.rejected] == ["VALUE.REQUIRED:first_name"]


def test_intake_loads_a_workbook_and_quarantines_a_macro_one(tmp_path, contract):
    """Through process_landing: rows reach bronze like CSV rows, the raw copy keeps the
    .xlsx extension, and a workbook with macros is quarantined whole."""
    from dip import BUCKET_LANDING, BUCKET_RAW
    from dip.intake import process_landing
    from dip.storage import LocalStore
    from tests.unit.test_intake import FakeWarehouse

    excel = Contract(
        contract.feed,
        contract.version,
        {**contract.spec, "source": {**contract.spec["source"], "filename_pattern": "elig_*.xlsx"}},
        contract.path,
    )
    store, wh = LocalStore(tmp_path), FakeWarehouse()
    store.put(BUCKET_LANDING, f"{excel.feed}/elig_20261021.xlsx", workbook(HEADER, GOOD))
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook(HEADER, GOOD, title_rows=[["x"]]))) as src, (
        zipfile.ZipFile(buf, "w")
    ) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/vbaProject.bin", b"\x00fake")
    store.put(BUCKET_LANDING, f"{excel.feed}/elig_20261020.xlsx", buf.getvalue())
    out = {
        o.source_key.split("/")[-1]: o
        for o in process_landing(store, wh, {excel.feed: excel}, NOW)
    }
    assert (out["elig_20261021.xlsx"].status, out["elig_20261021.xlsx"].accepted) == ("loaded", 1)
    assert out["elig_20261020.xlsx"].reason_codes == ("FILE.MACROS",)
    assert [r.record["member_id"] for r in wh.csv_rows] == ["M000000001"]
    assert out["elig_20261021.xlsx"].raw_key.endswith(".xlsx")
    assert store.get(BUCKET_RAW, out["elig_20261021.xlsx"].raw_key)
