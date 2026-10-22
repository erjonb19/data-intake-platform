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
rejects the file (`FILE.ROW_VIOLATIONS`). Either way every bad row keeps its own reason.

Values are checked and stored trimmed of surrounding whitespace, otherwise as received
(text); silver models type them. `check_contract` catches contract mistakes (a bad regex,
an unknown encoding, a bound or allowed value of the wrong type) when the contract loads,
not when a file arrives.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dip.fhir import Violation

BOM = "\ufeff"


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
    """A contract `min`/`max` as a value comparable with the column's typed values.
    `today` (date columns) is the UTC date the file was received."""
    if col_type == "date" and raw == "today":
        return today
    if isinstance(raw, bool) or col_type in ("string", "boolean"):
        raise ValueError(f"min/max does not apply to {col_type} columns")
    return _parse(str(raw), col_type)


def _allowed(col: dict[str, Any]) -> list[Any]:
    col_type = col.get("type", "string")
    return [_parse(str(a).lower() if col_type == "boolean" else str(a), col_type)
            for a in col["allowed"]]  # fmt: skip


def check_contract(spec: dict[str, Any]) -> list[str]:
    """Problems with a csv contract that the JSON Schema cannot express."""
    problems = []
    encoding = spec["format"].get("encoding", "utf-8")
    try:
        codecs.lookup(encoding)
    except LookupError:
        problems.append(f"format.encoding: unknown encoding {encoding!r}")
    today = date(2000, 1, 1)
    for col in spec.get("columns", []):
        where, col_type = f"columns.{col['name']}", col.get("type", "string")
        if "pattern" in col:
            try:
                re.compile(col["pattern"])
            except re.error as exc:
                problems.append(f"{where}.pattern: {exc}")
        for key in ("min", "max"):
            if key in col:
                try:
                    _bound(col[key], col_type, today)
                except ValueError:
                    problems.append(f"{where}.{key}: {col[key]!r} is not a valid {col_type} bound")
        if "allowed" in col:
            try:
                _allowed(col)
            except ValueError:
                problems.append(f"{where}.allowed: values must be valid {col_type} values")
    return problems


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
    if "allowed" in col and typed not in _allowed(col):
        return Violation(f"VALUE.NOT_ALLOWED:{name}", f"{name} is not one of {col['allowed']}")
    if "min" in col and typed < _bound(col["min"], col_type, today):
        return Violation(f"VALUE.OUT_OF_RANGE:{name}", f"{name} is below {col['min']}")
    if "max" in col and typed > _bound(col["max"], col_type, today):
        return Violation(f"VALUE.OUT_OF_RANGE:{name}", f"{name} is above {col['max']}")
    return None


def _header_violations(header: list[str], spec: dict[str, Any]) -> list[Violation]:
    expected = [c["name"] for c in spec["columns"]]
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
    if not spec.get("allow_extra_columns", False):
        out += [
            Violation(f"SCHEMA.UNEXPECTED_COLUMN:{name}", f"column {name!r} is not in the contract")
            for name in header
            if name not in expected
        ]
    return out


def _mark_duplicates(
    checked: list[tuple[Row, Violation | None]], name: str
) -> list[tuple[Row, Violation | None]]:
    """Every row sharing a non-empty key is rejected, because the file does not say which
    one is right. Keys are counted over all rows that have a value for the column, so a
    copy that fails another check still makes the other copy ambiguous; a row already
    rejected keeps its first reason."""
    keys = Counter(r.record.get(name, "") for r, _ in checked)
    keys.pop("", None)
    return [
        (r, v)
        if v is not None or keys[r.record.get(name, "")] < 2
        else (r, Violation(f"KEY.DUPLICATE:{name}", f"{name} appears more than once"))
        for r, v in checked
    ]


def validate_csv(data: bytes, contract: Any, received_at: datetime | None = None) -> CsvResult:
    spec = contract.spec
    fmt = spec["format"]
    today = (received_at or datetime.now(UTC)).astimezone(UTC).date()
    result = CsvResult()
    try:
        text = data.decode(fmt.get("encoding", "utf-8"))
    except UnicodeDecodeError as exc:
        result.file_violations.append(
            Violation("FILE.ENCODING", f"not valid {fmt.get('encoding', 'utf-8')}: {exc.reason}")
        )
        return result
    text = text.removeprefix(BOM)
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
    # Only truly blank lines (often a trailing newline) are dropped and counted. A row of
    # empty fields such as ",,,," is data and goes through the checks.
    result.skipped["blank_line"] = sum(1 for r in rows if not r)
    rows = [r for r in rows if r]
    if not rows:
        result.file_violations.append(Violation("FILE.EMPTY", "no header row"))
        return result

    header = [h.strip() for h in rows[0]]
    result.file_violations.extend(_header_violations(header, spec))
    if result.file_violations:
        return result

    columns = spec["columns"]
    checked: list[tuple[Row, Violation | None]] = []
    for n, values in enumerate(rows[1:], start=1):
        values = [v.strip() for v in values]
        if len(values) != len(header):
            row = Row(n, dict(zip(header, values, strict=False)))
            detail = f"{len(values)} fields, header has {len(header)}"
            checked.append((row, Violation("ROW.FIELD_COUNT", detail)))
            continue
        row = Row(n, dict(zip(header, values, strict=True)))
        violation = None
        for col in columns:
            violation = _check_value(col, row.record[col["name"]], today)
            if violation:
                break
        checked.append((row, violation))
    for col in (c for c in columns if c.get("unique")):
        checked = _mark_duplicates(checked, col["name"])

    result.accepted = [r for r, v in checked if v is None]
    result.rejected = [(r, v) for r, v in checked if v is not None]
    total = len(checked)
    min_rows = spec.get("min_rows", 1)
    max_ratio = float(spec.get("max_bad_row_ratio", 0.0))
    if total < min_rows:
        result.file_violations.append(
            Violation("FILE.TOO_FEW_ROWS", f"{total} data rows, contract requires {min_rows}")
        )
    elif result.rejected and spec["on_row_violation"] == "reject_file":
        first = result.rejected[0][1].reason_code
        result.file_violations.append(
            Violation(
                "FILE.ROW_VIOLATIONS",
                f"{len(result.rejected)} of {total} rows failed (first: {first})",
            )
        )
    elif total and len(result.rejected) / total > max_ratio:
        result.file_violations.append(
            Violation(
                "FILE.BAD_ROW_RATIO",
                f"{len(result.rejected)} of {total} rows failed, above the contract's"
                f" max_bad_row_ratio of {max_ratio}",
            )
        )
    if result.file_violations:
        # The file is quarantined whole: nothing loads, and the intake records each bad
        # row's reason alongside the file-level one.
        result.accepted = []
    return result
