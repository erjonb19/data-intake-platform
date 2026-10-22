"""Load relational source tables (CSV) into the warehouse under a per-table contract (M2).

The contract names each table's file, exact columns, Postgres types, primary key and
not-null columns. For every table:

  - header check: columns must match the contract exactly, in order, otherwise the table
    is quarantined with SCHEMA.MISSING_COLUMN:<col>, SCHEMA.UNEXPECTED_COLUMN:<col> or
    SCHEMA.COLUMN_ORDER (schema drift is caught by code, as for file feeds)
  - typed load: COPY into a typed table; a value that does not fit its type quarantines the
    table with VALUE.TYPE_MISMATCH
  - key and not-null checks after loading: KEY.DUPLICATE, VALUE.REQUIRED:<col>

Results are recorded in ops.relational_load, one row per table. Tables that fail are
dropped, so nothing downstream reads a half-valid table. Reloading drops dependent dbt
views too (CASCADE); the dbt run that follows every load rebuilds them.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DDL = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.relational_load (
    source         text        NOT NULL,
    table_name     text        NOT NULL,
    file_sha256    text        NOT NULL,
    status         text        NOT NULL CHECK (status IN ('loaded', 'quarantined')),
    row_count      bigint,
    reason_codes   text[]      NOT NULL DEFAULT '{}',
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, table_name, file_sha256)
);
"""


@dataclass
class TableResult:
    table: str
    file_sha256: str
    status: str = "loaded"
    rows: int = 0
    reason_codes: list[str] = field(default_factory=list)


def load_contract(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text("utf-8"))


def _read_header(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


def header_violations(expected: list[str], found: list[str]) -> list[str]:
    missing = [c for c in expected if c not in found]
    extra = [c for c in found if c not in expected]
    codes = [f"SCHEMA.MISSING_COLUMN:{c}" for c in missing]
    codes += [f"SCHEMA.UNEXPECTED_COLUMN:{c}" for c in extra]
    if not codes and found != expected:
        codes.append("SCHEMA.COLUMN_ORDER")
    return codes


def load_table(conn: Any, contract: dict, table: str, root: Path) -> TableResult:
    spec = contract["tables"][table]
    schema = contract["schema"]
    path = root / spec["file"]
    data = path.read_bytes()
    result = TableResult(table, hashlib.sha256(data).hexdigest())
    columns = list(spec["columns"])
    qualified = f"{schema}.{table}"

    def quarantine(*codes: str) -> TableResult:
        result.status, result.reason_codes = "quarantined", list(codes)
        with conn.cursor() as cur:
            cur.execute(f"drop table if exists {qualified} cascade")
        conn.commit()
        return result

    if codes := header_violations(columns, _read_header(path)):
        return quarantine(*codes)
    cols_sql = ", ".join(f"{c} {t}" for c, t in spec["columns"].items())
    with conn.cursor() as cur:
        cur.execute(f"create schema if not exists {schema}")
        cur.execute(f"drop table if exists {qualified} cascade")
        cur.execute(f"create table {qualified} ({cols_sql})")
    conn.commit()
    try:
        with (
            conn.cursor() as cur,
            cur.copy(
                f"copy {qualified} ({', '.join(columns)}) from stdin with (format csv, header true)"
            ) as copy,
        ):
            copy.write(gzip.decompress(data))
        conn.commit()
    except Exception as exc:  # a value that does not fit its declared type
        conn.rollback()
        return quarantine(f"VALUE.TYPE_MISMATCH:{type(exc).__name__}")

    checks: list[str] = []
    with conn.cursor() as cur:
        for col in spec.get("not_null", []):
            cur.execute(f"select count(*) from {qualified} where {col} is null")
            if cur.fetchone()[0]:
                checks.append(f"VALUE.REQUIRED:{col}")
        if pk := spec.get("primary_key"):
            keys = ", ".join(pk)
            cur.execute(
                f"select count(*) from (select {keys} from {qualified}"
                f" group by {keys} having count(*) > 1) as d"
            )
            if cur.fetchone()[0]:
                checks.append("KEY.DUPLICATE")
        cur.execute(f"select count(*) from {qualified}")
        result.rows = cur.fetchone()[0]
    conn.commit()
    if checks:
        return quarantine(*checks)
    return result


def load_all(conn: Any, contract: dict, root: Path) -> list[TableResult]:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    results = [load_table(conn, contract, t, root) for t in contract["tables"]]
    with conn.cursor() as cur:
        for r in results:
            cur.execute(
                "insert into ops.relational_load (source, table_name, file_sha256, status,"
                " row_count, reason_codes) values (%s, %s, %s, %s, %s, %s)"
                " on conflict (source, table_name, file_sha256) do update set"
                " status = excluded.status, row_count = excluded.row_count,"
                " reason_codes = excluded.reason_codes, loaded_at = now()",
                (contract["source"], r.table, r.file_sha256, r.status, r.rows, r.reason_codes),
            )
    conn.commit()
    return results


def csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    """A gzipped CSV in memory (for tests)."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return gzip.compress(buf.getvalue().encode("utf-8"))
