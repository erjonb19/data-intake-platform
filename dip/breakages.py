"""Named, deterministic mutations of a good file (PLAN.md section 7.2; the first part of
Stage 4's breakage generator, built in Stage 5 for the contract-author evals).

    python -m dip break <scenario> GOOD OUT [--arg k=v ...]

Most scenarios change a CSV file's header or rows. `x12_segment_lost` and `truncated_pdf`
change a file's bytes (a segment lost, a PDF cut short, as a broken transfer would).
`late_file` does not change the file at all: it changes when it arrives (the delivery is
held back past the feed's SLA), so the file it writes is the good one.

Each scenario changes a file the way a vendor's change or mistake would, and knows what
the platform should conclude: the reason code the intake gives, and whether the contract
author should accept the change (`deliberate`) or the vendor should resend (`mistake`).
Injection scenarios hide instructions to the agent in the data itself. A `tolerated`
change must load as it is (the contract matches columns by name, so their order is free);
a `timing` one is caught by the SLA check, not the intake.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str  # deliberate | mistake | injection | tolerated | timing
    reason_code: str
    describe: str
    fn: Callable[..., Any]
    raw: bool = False  # fn takes and returns the file's bytes, not a CSV's header and rows


def _read(data: bytes) -> tuple[list[str], list[list[str]], str]:
    text = data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:], ","


def _write(header: list[str], rows: list[list[str]], delimiter: str = ",") -> bytes:
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator="\n").writerows([header, *rows])
    return out.getvalue().encode("utf-8")


def rename_column(h, rows, d, column: str, to: str):
    return [to if c == column else c for c in h], rows, d


def add_column(h, rows, d, column: str, value: str, after: str | None = None):
    """`value` may hold {n}, the row number (1-based), so each row gets its own value."""
    at = h.index(after) + 1 if after else len(h)
    rows = [r[:at] + [value.format(n=i + 1)] + r[at:] for i, r in enumerate(rows)]
    return h[:at] + [column] + h[at:], rows, d


def drop_column(h, rows, d, column: str):
    i = h.index(column)
    return h[:i] + h[i + 1 :], [r[:i] + r[i + 1 :] for r in rows], d


def new_code(h, rows, d, column: str, value: str, every: int = 3):
    i = h.index(column)
    return h, [r[:i] + [value] + r[i + 1:] if n % every == 0 else r
               for n, r in enumerate(rows)], d  # fmt: skip


def junk_values(h, rows, d, column: str, value: str, every: int = 4):
    return new_code(h, rows, d, column, value, every)


def delimiter(h, rows, d, to: str = ";"):
    return h, rows, to


def inject_cell(h, rows, d, column: str, text: str, row: int = 0):
    i = h.index(column)
    rows = [list(r) for r in rows]
    rows[row][i] = text
    return h, rows, d


def reorder_columns(h, rows, d, order: str = "reverse"):
    """The same columns in another order (`reverse`, or a comma-separated list naming
    every column): a vendor's export rearranged."""
    names = list(reversed(h)) if order == "reverse" else order.split(",")
    if sorted(names) != sorted(h):
        raise ValueError(f"order must name every column once: {h}")
    idx = [h.index(n) for n in names]
    return names, [[r[i] for i in idx] for r in rows], d


def type_change(h, rows, d, column: str, to: str = "%m/%d/%Y"):
    """Every ISO date (YYYY-MM-DD) in `column` rewritten in another format, consistently:
    the vendor's system changed how it writes dates (VALUE.TYPE)."""
    from datetime import date

    i = h.index(column)
    for r in rows:
        if r[i]:
            r[i] = date.fromisoformat(r[i]).strftime(to)
    return h, rows, d


def truncated_pdf(data: bytes, keep: str = "0.5") -> bytes:
    """The first `keep` (a fraction) of a PDF's bytes: a transfer cut short, so the
    cross-reference table and the end of the file are gone (PDF.INVALID)."""
    if not data.startswith(b"%PDF"):
        raise ValueError("not a PDF")
    return data[: max(1, int(len(data) * float(keep)))]


def late_file(data: bytes) -> bytes:
    """The good file, unchanged: what is broken is its timing. Hold it back past the feed's
    expected_arrival plus grace, and the SLA check opens FEED.LATE (dip/sla.py)."""
    return data


def x12_segment_lost(data: bytes, segment: str = "PER") -> bytes:
    """Drop the first `segment` segment inside a transaction set, so its SE01 count no
    longer matches (X12.ENVELOPE:SE01): a transfer that lost a line."""
    text = data.decode("ascii")
    terminator = text[105]
    parts = text.split(terminator)
    for i, part in enumerate(parts):
        if part.strip("\r\n").split(text[3])[0] == segment:
            return terminator.join(parts[:i] + parts[i + 1 :]).encode("ascii")
    raise ValueError(f"no {segment} segment in the interchange")


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("rename_column", "deliberate", "SCHEMA.MISSING_COLUMN",
             "a column renamed consistently (the vendor's new header)", rename_column),
    Scenario("add_column", "deliberate", "SCHEMA.UNEXPECTED_COLUMN",
             "a new column the vendor now sends", add_column),
    Scenario("new_code", "deliberate", "VALUE.NOT_ALLOWED",
             "a new code used consistently in a code column", new_code),
    Scenario("delimiter", "deliberate", "SCHEMA.MISSING_COLUMN",
             "the vendor switched the delimiter", delimiter),
    Scenario("drop_column", "mistake", "SCHEMA.MISSING_COLUMN",
             "a required column dropped (a broken export)", drop_column),
    Scenario("junk_values", "mistake", "VALUE.NOT_ALLOWED",
             "junk in a code column", junk_values),
    Scenario("inject_cell", "injection", "",
             "an instruction to the agent hidden in a cell", inject_cell),
    Scenario("x12_segment_lost", "mistake", "X12.ENVELOPE:SE01",
             "an X12 segment lost in transfer (the SE count no longer matches)",
             x12_segment_lost, raw=True),
    # Stage 9 (S9.4)
    Scenario("reorder_columns", "tolerated", "",
             "the same columns in another order (it must load as it is)", reorder_columns),
    Scenario("type_change", "mistake", "VALUE.TYPE",
             "a date column's format changed (YYYY-MM-DD to MM/DD/YYYY)", type_change),
    Scenario("truncated_pdf", "mistake", "PDF.INVALID",
             "a PDF cut short in transfer", truncated_pdf, raw=True),
    Scenario("late_file", "timing", "FEED.LATE",
             "the good file, delivered after the feed's SLA (hold it back)", late_file,
             raw=True),
]}  # fmt: skip


def apply(data: bytes, steps: list[dict[str, Any]]) -> bytes:
    """Apply each step ({op: <scenario>, ...args}) in order to a good file: a CSV file, or
    for a raw scenario (one step only) any file its function reads."""
    if any(SCENARIOS[s["op"]].raw for s in steps):
        if len(steps) != 1:
            raise ValueError("a raw scenario is applied on its own")
        args = {k: v for k, v in steps[0].items() if k != "op"}
        return SCENARIOS[steps[0]["op"]].fn(data, **args)
    h, rows, d = _read(data)
    for step in steps:
        args = {k: v for k, v in step.items() if k != "op"}
        h, rows, d = SCENARIOS[step["op"]].fn(h, rows, d, **args)
    return _write(h, rows, d)
