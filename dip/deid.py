"""De-identification key handling (R4).

The shareable OMOP tier is derived from the restricted tier with a secret key: HMAC-SHA256
pseudonyms for every id and a per-person date shift. The key comes from DIP_DEID_KEY (a
Kubernetes Secret in the cluster, `.env` locally) and is stored in `ops_secret.deid_key`,
so dbt reads it inside SQL and it never appears in compiled SQL or logs. It reaches Postgres
as COPY data, never as statement text or a bind parameter, so `log_statement = all`,
`log_min_duration_statement` and `pg_stat_activity` cannot show it.

Changing the key changes every pseudonym and shift, so extracts are only comparable when
they were built with the same key. The key's fingerprint (not the key) goes into extract
metadata so this is checkable.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

MIN_KEY_LENGTH = 32
BS = chr(92)  # backslash


def fingerprint(key: str) -> str:
    return hashlib.sha256(("dip-deid-fingerprint:" + key).encode("utf-8")).hexdigest()[:16]


def install_key(conn: Any, key: str | None, key_id: str = "default") -> str:
    """Store the key (idempotent) and return its fingerprint."""
    if not key or len(key) < MIN_KEY_LENGTH:
        raise ValueError(
            f"DIP_DEID_KEY must be set and at least {MIN_KEY_LENGTH} characters"
            " (generate one with: python -c 'import secrets; print(secrets.token_hex(32))')"
        )
    fp = fingerprint(key)
    try:
        with conn.cursor() as cur:
            cur.execute("create temporary table deid_key_in (key text not null) on commit drop")
            _copy_value(cur, "copy deid_key_in (key) from stdin", key)
            cur.execute(
                "insert into ops_secret.deid_key (key_id, key, fingerprint)"
                " select %s, key, %s from deid_key_in"
                " on conflict (key_id) do update set key = excluded.key,"
                " fingerprint = excluded.fingerprint, installed_at = now()"
                " where ops_secret.deid_key.fingerprint <> excluded.fingerprint",
                (key_id, fp),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fp


def _copy_value(cur: Any, sql: str, value: str) -> None:
    """COPY one text value: psycopg 3 (the CLI, tests) or psycopg2 (Airflow's hook)."""
    if hasattr(cur, "copy"):
        with cur.copy(sql) as copy:
            copy.write_row((value,))
        return
    # psycopg2 has no row writer: COPY text format, escaping its four special characters.
    for char, esc in ((BS, BS + BS), ("\t", BS + "t"), ("\n", BS + "n"), ("\r", BS + "r")):
        value = value.replace(char, esc)
    cur.copy_expert(sql, io.StringIO(value + "\n"))
