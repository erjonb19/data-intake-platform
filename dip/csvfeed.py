"""Validate a delimited text file (CSV) against a `csv` data contract (Stage 1).

File-level violations quarantine the whole file: it cannot be decoded, has no header, or
its header drifted from the contract (`SCHEMA.MISSING_COLUMN:<col>`,
`SCHEMA.UNEXPECTED_COLUMN:<col>`, `SCHEMA.DUPLICATE_COLUMN:<col>`). Schema drift is caught
here by plain code; nothing downstream guesses at a changed layout.

Row-level checks run per column (`VALUE.REQUIRED`, `VALUE.TYPE`, `VALUE.PATTERN`,
`VALUE.NOT_ALLOWED`, `VALUE.OUT_OF_RANGE`, `KEY.DUPLICATE`, each suffixed with the column)
plus `ROW.FIELD_COUNT`. With `on_row_violation: quarantine_row` the bad rows are
quarantined and the good rows continue, unless more than `max_bad_row_ratio` of the rows
are bad, which rejects the file (`FILE.BAD_ROW_RATIO`). With `reject_file`, any bad row
rejects the file (`FILE.ROW_VIOLATIONS`).

Accepted rows keep their values exactly as received (text); silver models type them.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dip.contracts import Contract
from dip.fhir import Violation


@dataclass(frozen=True)
class Row:
    row_number: int  # 1-based data row (the header is row 0)
    record: dict[str, str]


@dataclass
class CsvResult:
    file_violations: list[Violation] = field(default_factory=list)
    accepted: list[Row] = field(default_factory=list)
    rejected: list[tuple[Row, Violation]] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def file_rejected(self) -> bool:
        return bool(self.file_violations)

    @property
    def patient_id(self) -> str | None:
        return None


def _parse(value: str, col_type: str) -> Any:
    """The typed value, or raises ValueError."""
    if col_type == "string":
        return value
    if col_type == "integer":
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise ValueError(value)
        return int(value)
    if col_type == "decimal":
        try:
            d = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(value) from exc
        if not d.is_finite():
            raise ValueError(value)
        return d
    if col_type == "date":
        # ISO 8601 calendar dates only: one unambiguous format per contract.
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError(value)
        return date.fromisoformat(value)
    if col_type == "boolean":
        if value.lower() not in ("true", "false"):
            raise ValueError(value)
        return value.lower() == "true"
    raise ValueError(f"unknown type {col_type}")


def _bound(raw: Any, col_type: str, today: date) -> Any:
    if col_type == "date":
        return today if raw == "today" else date.fromisoformat(str(raw))
    if col_type == "decimal":
        return Decimal(str(raw))
    return raw


def _check_value(col: dict[str, Any], value: str, today: date) -> Violation | None:
    name, col_type = col["name"], col.get("type", "string")
    if value == "":
        if col.get("required"):
            return Violation(f"VALUE.REQUIRED:{name}", f"{name} is empty")
        return None
    try:
        typed = _parse(value, col_type)
    except ValueError:
        return Violation(f"VALUE.TYPE:{name}", f"{name} is not a valid {col_type}")
    if "pattern" in col and not re.fullmatch(col["pattern"], value):
        return Violation(f"VALUE.PATTERN:{name}", f"{name} does not match {col['pattern']}")
    if "allowed" in col and typed not in col["allowed"]:
        return Violation(f"VALUE.NOT_ALLOWED:{name}", f"{name} is not one of {col['allowed']}")
    if "min" in col and typed < _bound(col["min"], col_type, today):
        return Violation(f"VALUE.OUT_OF_RANGE:{name}", f"{name} is below {col['min']}")
    if "max" in col and typed > _bound(col["max"], col_type, today):
        return Violation(f"VALUE.OUT_OF_RANGE:{name}", f"{name} is above {col['max']}")
    return None


def _header_violations(header: list[str], contract: Contract) -> list[Violation]:
    expected = [c["name"] for c in contract.spec["columns"]]
    out = [
        Violation(f"SCHEMA.DUPLICATE_COLUMN:{name}", f"column {name!r} appears {n} times")
        for name, n in sorted(Counter(header).items())
        if n > 1
    ]
    out += [
        Violation(f"SCHEMA.MISSING_COLUMN:{name}", f"column {name!r} is missing")
        for name in expected
        if name not in header
    ]
    if not contract.spec.get("allow_extra_columns", False):
        out += [
            Violation(f"SCHEMA.UNEXPECTED_COLUMN:{name}", f"column {name!r} is not in the contract")
            for name in header
            if name not in expected
        ]
    return out


def validate_csv(data: bytes, contract: Contract, received_at: datetime | None = None) -> CsvResult:
    fmt = contract.spec["format"]
    today = (received_at or datetime.now(UTC)).date()
    result = CsvResult()
    try:
        text = data.decode(fmt.get("encoding", "utf-8"))
    except UnicodeDecodeError as exc:
        result.file_violations.append(
            Violation("FILE.ENCODING", f"not valid {fmt.get('encoding', 'utf-8')}: {exc.reason}")
        )
        return result
    text = text.removeprefix("﻿")
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=fmt.get("delimiter", ","),
        quotechar=fmt.get("quotechar", '"'),
        strict=True,
    )
    try:
        rows = list(reader)
    except csv.Error as exc:
        result.file_violations.append(
            Violation("FILE.PARSE_ERROR", f"line {reader.line_num}: {exc}")
        )
        return result
    # Blank lines (often a trailing newline) carry no data.
    rows = [r for r in rows if any(v.strip() for v in r)]
    if not rows:
        result.file_violations.append(Violation("FILE.EMPTY", "no header row"))
        return result

    header = [h.strip() for h in rows[0]]
    result.file_violations.extend(_header_violations(header, contract))
    if result.file_violations:
        return result

    columns = contract.spec["columns"]
    checked: list[tuple[Row, Violation | None]] = []
    for n, values in enumerate(rows[1:], start=1):
        if len(values) != len(header):
            row = Row(n, dict(zip(header, values, strict=False)))
            checked.append(
                (
                    row,
                    Violation("ROW.FIELD_COUNT", f"{len(values)} fields, header has {len(header)}"),
                )
            )
            continue
        row = Row(n, dict(zip(header, values, strict=True)))
        violation = None
        for col in columns:
            violation = _check_value(col, row.record[col["name"]].strip(), today)
            if violation:
                break
        checked.append((row, violation))

    # Uniqueness over the rows that passed: every row sharing a duplicated key is
    # rejected, because the file does not say which one is right.
    for col in (c for c in columns if c.get("unique")):
        name = col["name"]
        counts = Counter(r.record[name].strip() for r, v in checked if v is None)
        checked = [
            (r, v)
            if v is not None or counts[r.record[name].strip()] < 2
            else (r, Violation(f"KEY.DUPLICATE:{name}", f"{name} appears more than once"))
            for r, v in checked
        ]

    result.accepted = [r for r, v in checked if v is None]
    result.rejected = [(r, v) for r, v in checked if v is not None]
    total = len(checked)
    min_rows = contract.spec.get("min_rows", 1)
    if total < min_rows:
        result.file_violations.append(
            Violation("FILE.TOO_FEW_ROWS", f"{total} data rows, contract requires {min_rows}")
        )
    elif result.rejected and contract.rejects_file_on_row_violation:
        first = result.rejected[0][1].reason_code
        result.file_violations.append(
            Violation(
                "FILE.ROW_VIOLATIONS",
                f"{len(result.rejected)} of {total} rows failed (first: {first})",
            )
        )
    elif total and len(result.rejected) / total > contract.max_bad_row_ratio:
        result.file_violations.append(
            Violation(
                "FILE.BAD_ROW_RATIO",
                f"{len(result.rejected)} of {total} rows failed, above the contract's"
                f" max_bad_row_ratio of {contract.max_bad_row_ratio}",
            )
        )
    if result.file_violations:
        # The file is quarantined whole; per-row outcomes are reported, not loaded.
        result.accepted = []
    return result
