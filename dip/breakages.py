"""Named, deterministic mutations of a good CSV file (PLAN.md section 7.2; the first part of
Stage 4's breakage generator, built in Stage 5 for the contract-author evals).

    python -m dip break <scenario> GOOD.csv OUT.csv [--arg k=v ...]

Each scenario changes a file the way a vendor's change or mistake would, and knows what
the platform should conclude: the reason code the intake gives, and whether the contract
author should accept the change (`deliberate`) or the vendor should resend (`mistake`).
Injection scenarios hide instructions to the agent in the data itself.
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
    kind: str  # deliberate | mistake | injection
    reason_code: str
    describe: str
    fn: Callable[..., tuple[list[str], list[list[str]], str]]


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
    at = h.index(after) + 1 if after else len(h)
    return h[:at] + [column] + h[at:], [r[:at] + [value] + r[at:] for r in rows], d


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
]}  # fmt: skip


def apply(data: bytes, steps: list[dict[str, Any]]) -> bytes:
    """Apply each step ({op: <scenario>, ...args}) in order to a good CSV file."""
    h, rows, d = _read(data)
    for step in steps:
        args = {k: v for k, v in step.items() if k != "op"}
        h, rows, d = SCENARIOS[step["op"]].fn(h, rows, d, **args)
    return _write(h, rows, d)
