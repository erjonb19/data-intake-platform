"""What the contract-author agent (Stage 5) works from: a file observed against a contract,
a candidate contract dry-run over that file, and what a candidate loosens.

Plain code, no model: the agent's tools (dip/agents/author_tools.py) call these, and the
evals score with the same functions, so "valid" and "loosened" mean one thing everywhere.

PHI: an observation shows values only for columns the contract declares and does not mark
`phi`. A column the contract does not know may be PHI under a new name ("DOB"), so it gets
counts, an inferred type and value shapes ("A999999999"); its values are shown only when
they are a short set of code-like tokens (at most 12 of them, each up to 8 upper-case
letters and digits with at least one letter, or at most 3 digits), which a name, a date,
a ZIP code or an identifier never is.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

from dip.contracts import Contract

MAX_EXAMPLES = 3
MAX_CODES = 12
# Code-like: up to 8 upper-case letters, digits, _ or -, with at least one letter (plan,
# currency, unit codes), or at most 3 digits (place of service, days supply). Never a
# YYYYMMDD date, a ZIP code or an identifier, which are longer runs of digits.
CODE = re.compile(r"^(?=[A-Z0-9_-]*[A-Z])[A-Z0-9_-]{1,8}$|^[0-9]{1,3}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INTEGER = re.compile(r"^-?\d+$")
DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


def _table(data: bytes, fmt: dict[str, Any]) -> tuple[list[str], list[list[str]], dict]:
    """Header and rows, read as the intake reads the format, plus what the bytes say about
    themselves (encoding, the delimiter a sniffer sees)."""
    facts: dict[str, Any] = {}
    if fmt["type"] == "excel":
        from dip.csvfeed import CsvResult
        from dip.excelfeed import _normalize, _rows

        r = CsvResult()
        try:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
            facts["sheets"] = wb.sheetnames
            wb.close()
        except Exception:  # noqa: BLE001 - _rows reports why below
            pass
        rows = _rows(data, fmt, r)
        if rows is None:
            facts["unreadable"] = [v.reason_code for v in r.file_violations]
            return [], [], facts
        rows = _normalize(rows, r)
    else:
        encoding = fmt.get("encoding", "utf-8")
        try:
            text = data.decode(encoding)
            facts["encoding"] = encoding
        except UnicodeDecodeError:
            text = data.decode("latin-1")
            facts["encoding"] = f"not {encoding} (read as latin-1)"
        sample = text[:4096]
        try:
            facts["sniffed_delimiter"] = csv.Sniffer().sniff(sample, ",;|\t").delimiter
        except csv.Error:
            facts["sniffed_delimiter"] = None
        text = text.removeprefix("\ufeff")  # as the intake reads it (csvfeed)
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=fmt.get("delimiter", ","),
                               quotechar=fmt.get("quotechar", '"')))  # fmt: skip
    if not rows:
        return [], [], facts
    return [h.strip() for h in rows[0]], rows[1:], facts


def shape(value: str) -> str:
    """A value's shape: letters A (a for lower case), digits 9, ASCII punctuation and spaces
    kept, max 32. Any other character (a letter or digit of another script included) is
    mapped too, never kept: a shape carries no text (Stage 10, S10.4)."""

    def one(ch: str) -> str:
        if ch.isalpha():
            return "a" if ch.islower() else "A"
        if ch.isdigit():
            return "9"
        return ch if ch.isascii() else "?"

    return "".join(one(ch) for ch in value[:32])


def _infer(values: list[str]) -> str:
    seen = [v for v in values if v != ""]
    if not seen:
        return "empty"
    for name, rx in (("integer", INTEGER), ("decimal", DECIMAL), ("date", DATE)):
        if all(rx.match(v) for v in seen):
            return name
    return "string"


def observe(contract: Contract | None, data: bytes, fmt: dict[str, Any] | None = None) -> dict:
    """The file against the contract (None: a new vendor, nothing declared yet)."""
    fmt = fmt or (contract.spec["format"] if contract else {"type": "csv"})
    header, rows, facts = _table(data, fmt)
    declared = {c["name"]: c for c in (contract.spec.get("columns", []) if contract else [])}
    columns = []
    for i, name in enumerate(header):
        values = [r[i].strip() if i < len(r) else "" for r in rows]
        spec = declared.get(name)
        safe = spec is not None and not spec.get("phi")
        item: dict[str, Any] = {
            "column": name, "declared": spec is not None,
            "phi": bool(spec and spec.get("phi")), "inferred_type": _infer(values),
            "empty": sum(1 for v in values if v == ""), "distinct": len(set(values) - {""}),
        }  # fmt: skip
        if spec:
            item["declared_type"] = spec.get("type", "string")
            item["rule_failures"] = _rule_failures(spec, values, safe)
        if safe:
            common = Counter(v for v in values if v).most_common(5)
            item["top_values"] = [[v[:60], n] for v, n in common]
        elif not item["phi"]:
            # Undeclared: the value shapes (letters as A, digits as 9), and the values
            # themselves only when they look like codes (a short, upper-case or numeric
            # set): enough to write a pattern or an allowed list, never a name or a date.
            item["shapes"] = [[sh, n] for sh, n in Counter(
                shape(v) for v in values if v).most_common(5)]  # fmt: skip
            codes = sorted(set(values) - {""})
            if codes and len(codes) <= MAX_CODES and len(rows) >= 2 * len(codes) and all(
                    CODE.match(v) for v in codes):  # fmt: skip
                item["codes"] = codes
        columns.append(item)
    return {
        "rows": len(rows),
        "file": facts,
        "columns": columns,
        "missing_declared": [n for n in declared if n not in header],
        "unexpected": [n for n in header if n not in declared],
        "order_differs": [n for n in header if n in declared] != [n for n in declared
                                                                    if n in header],
    }  # fmt: skip


def _rule_failures(spec: dict, values: list[str], show: bool) -> dict[str, Any]:
    """How many values break each of the column's rules, with examples when not PHI."""
    out: dict[str, Any] = {}

    def note(rule: str, value: str) -> None:
        entry = out.setdefault(rule, {"count": 0, "examples": []})
        entry["count"] += 1
        if show and len(entry["examples"]) < MAX_EXAMPLES and value not in entry["examples"]:
            entry["examples"].append(value[:60])

    t = spec.get("type", "string")
    for v in values:
        if v == "":
            if spec.get("required"):
                note("required", v)
            continue
        if t == "integer" and not INTEGER.match(v) or t == "decimal" and not DECIMAL.match(v):
            note("type", v)
        if t == "date" and not DATE.match(v):
            note("type", v)
        if "allowed" in spec and v not in [str(a) for a in spec["allowed"]]:
            note("allowed", v)
        if "pattern" in spec and not re.match(spec["pattern"], v):
            note("pattern", v)
    return out


def dry_run(contract: Contract, data: bytes, at: datetime | None = None,
            filename: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The candidate contract over the file, as the intake would run it: the file name's
    pattern and date (when the name is known, against the time it was received), then the
    format's checks."""
    from dip.csvfeed import validate_csv
    from dip.excelfeed import validate_excel
    from dip.intake import _check_name

    if contract.format_type not in ("csv", "excel"):
        return {"error": f"dry runs cover CSV and Excel, not {contract.format_type}"}
    at = at or datetime.now(UTC)
    if filename is not None:
        _, name_violations = _check_name(filename, contract, at)
        if name_violations:
            return {"file_rejected": True, "accepted": 0, "rejected": 0,
                    "reason_codes": {v.reason_code: 1 for v in name_violations}}  # fmt: skip
    run = validate_excel if contract.format_type == "excel" else validate_csv
    r = run(data, contract, at)
    codes = Counter(v.reason_code for v in r.file_violations)
    codes.update(v.reason_code for _, v in r.rejected)
    # validate_* apply the file-level rules too (reject_file, max_bad_row_ratio, min_rows):
    # a file rejected whole has file violations and nothing accepted.
    return {
        "file_rejected": bool(r.file_violations),
        "accepted": len(r.accepted),
        "rejected": len(r.rejected),
        "reason_codes": dict(sorted(codes.items())),
    }


# What makes a contract accept more. Each entry is one rule the candidate drops or widens;
# defaults are the intake's (min_rows 1, allow_extra_columns false, max_bad_row_ratio 0).
CLASSIFICATION = ("public", "internal", "restricted")
WIDER_TYPE = {("integer", "decimal"), ("integer", "string"), ("decimal", "string"),
              ("date", "string")}  # fmt: skip


def loosening(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    if not old:
        return []
    out: list[dict[str, Any]] = []

    def add(where: str, rule: str, before: Any, after: Any) -> None:
        out.append({"where": where, "rule": rule, "from": before, "to": after})

    if new.get("max_bad_row_ratio", 0) > old.get("max_bad_row_ratio", 0):
        add("contract", "max_bad_row_ratio", old.get("max_bad_row_ratio", 0),
            new.get("max_bad_row_ratio"))  # fmt: skip
    if old.get("on_row_violation") == "reject_file" != new.get("on_row_violation"):
        add("contract", "on_row_violation", "reject_file", new.get("on_row_violation"))
    if new.get("min_rows", 1) < old.get("min_rows", 1):
        add("contract", "min_rows", old.get("min_rows", 1), new.get("min_rows", 1))
    if new.get("allow_extra_columns", False) and not old.get("allow_extra_columns", False):
        add("contract", "allow_extra_columns", False, True)
    oc, nc = old.get("classification"), new.get("classification")
    rank = {c: i for i, c in enumerate(CLASSIFICATION)}
    if oc in rank and rank.get(nc, -1) < rank[oc]:
        add("contract", "classification", oc, nc)
    os_, ns = old.get("source", {}), new.get("source", {})
    if set(ns.get("channels", [])) - set(os_.get("channels", [])):
        add("source", "channels", os_.get("channels"), ns.get("channels"))
    if ns.get("filename_pattern") != os_.get("filename_pattern"):
        add("source", "filename_pattern", os_.get("filename_pattern"), ns.get("filename_pattern"))
    od, nd = os_.get("filename_date"), ns.get("filename_date")
    if od and (not nd or nd.get("regex") != od.get("regex") or any(
            nd.get(k, d) > od.get(k, d) for k, d in (("max_days_before_received", 7),
                                                     ("max_days_after_received", 0)))):  # fmt: skip
        add("source", "filename_date", od, nd)
    new_cols = {c["name"]: c for c in new.get("columns", [])}
    for c in old.get("columns", []):
        n = new_cols.get(c["name"])
        if n is None:
            # A dropped column: a file without it now passes. For a rename the new column
            # carries the rules; the dropped one is still listed, for the person to see.
            if c.get("required"):
                add(c["name"], "dropped", "required column", None)
            continue
        if c.get("required") and not n.get("required"):
            add(c["name"], "required", True, n.get("required", False))
        if c.get("unique") and not n.get("unique"):
            add(c["name"], "unique", True, False)
        if c.get("phi") and not n.get("phi"):
            add(c["name"], "phi", True, False)
        if "pattern" in c and n.get("pattern") != c["pattern"]:
            add(c["name"], "pattern", c["pattern"], n.get("pattern"))
        if "allowed" in c:
            wider = set(map(str, n.get("allowed") or [])) - set(map(str, c["allowed"]))
            if wider or "allowed" not in n:
                add(c["name"], "allowed", c["allowed"], n.get("allowed", "<any value>"))
        if "min" in c and (n.get("min") is None or _lt(n["min"], c["min"])):
            add(c["name"], "min", c["min"], n.get("min"))
        if "max" in c and (n.get("max") is None or _lt(c["max"], n["max"])):
            add(c["name"], "max", c["max"], n.get("max"))
        if (c.get("type", "string"), n.get("type", "string")) in WIDER_TYPE:
            add(c["name"], "type", c.get("type"), n.get("type", "string"))
    return out


def _lt(a: Any, b: Any) -> bool:
    """a < b for numbers and dates; 'today' is the day it is checked (a moving bound, so a
    fixed future date is later than it, and a past date earlier)."""

    def norm(x: Any) -> Any:
        if x == "today":
            return date.today()
        if isinstance(x, str) and DATE.match(x):
            return date.fromisoformat(x)
        return x

    try:
        return norm(a) < norm(b)
    except TypeError:
        return str(a) < str(b)
