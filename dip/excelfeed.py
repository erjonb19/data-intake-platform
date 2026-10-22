"""Validate an Excel workbook (.xlsx) against an `excel` data contract (Stage 2).

The workbook is read with openpyxl in read-only mode, one sheet (`format.sheet`, else the
first), with the header on `format.header_row` (default 1; rows above it, such as a title,
are ignored). Cells become text the way a CSV export would write them, and then go
through exactly the CSV engine's header, row and file checks (`csvfeed.check_rows`), so a
problem gets the same reason code whichever format it arrived in:

  empty cell              ""
  whole number            "12" (Excel stores every number as a float; 12.0 becomes "12")
  other number            the shortest repr, "12.5"
  date or midnight time   "2026-10-22"; any other datetime "2026-10-22T08:30:00"
  boolean                 "true" / "false"
  formula                 its cached value. A formula never calculated (a file written by
                          a library, not saved by Excel) has none and reads as empty, so
                          a `required` column catches it

File-level reason codes added by this format:
  FILE.INVALID_XLSX       not a readable .xlsx workbook
  FILE.MACROS             the workbook contains a VBA project (.xlsm content): refused,
                          whatever the file is called
  SCHEMA.MISSING_SHEET:<name>
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, time
from typing import Any

from dip.csvfeed import CsvResult, check_rows
from dip.fhir import Violation


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.time() == time(0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip()


def _rows(data: bytes, fmt: dict[str, Any], result: CsvResult) -> list[list[str]] | None:
    import openpyxl

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if any(n.lower().endswith("vbaproject.bin") for n in z.namelist()):
                result.file_violations.append(
                    Violation("FILE.MACROS", "the workbook contains a VBA project (macros)")
                )
                return None
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        result.file_violations.append(
            Violation("FILE.INVALID_XLSX", f"not an .xlsx workbook: {exc}")
        )
        return None
    try:
        name = fmt.get("sheet")
        if name is not None and name not in wb.sheetnames:
            result.file_violations.append(
                Violation(f"SCHEMA.MISSING_SHEET:{name}", f"sheets are {wb.sheetnames}")
            )
            return None
        ws = wb[name] if name is not None else wb.worksheets[0]
        raw = [[cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return raw[fmt.get("header_row", 1) - 1 :]


def _normalize(rows: list[list[str]], result: CsvResult) -> list[list[str]]:
    """Excel pads every row to the sheet's widest column. Trailing empty header cells are
    not columns; data rows are cut back to the header's width when the extra cells are
    empty (a value out there stays, and fails ROW.FIELD_COUNT) and padded when short.
    Rows with no value at all are blank lines: dropped and counted, as in CSV."""
    if not rows:
        return rows
    header = list(rows[0])
    while header and header[-1] == "":
        header.pop()
    out = [header]
    blank = 0
    for row in rows[1:]:
        if not any(row):
            blank += 1
            continue
        row = list(row)
        while len(row) > len(header) and row[-1] == "":
            row.pop()
        row += [""] * (len(header) - len(row))
        out.append(row)
    result.skipped["blank_line"] = blank
    return out


def validate_excel(data: bytes, contract: Any, received_at: datetime | None = None) -> CsvResult:
    spec = contract.spec
    today = (received_at or datetime.now(UTC)).astimezone(UTC).date()
    result = CsvResult()
    rows = _rows(data, spec["format"], result)
    if rows is None:
        return result
    return check_rows(_normalize(rows, result), spec, today, result)
