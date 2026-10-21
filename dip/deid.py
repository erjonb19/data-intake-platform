"""De-identification key handling (R4).

The shareable OMOP tier is derived from the restricted tier with a secret key: HMAC-SHA256
pseudonyms for every id and a per-person date shift. The key comes from DIP_DEID_KEY (a
Kubernetes Secret in the cluster, `.env` locally) and is stored in `ops_secret.deid_key`,
so dbt reads it inside SQL and it never appears in compiled SQL or logs.

Changing the key changes every pseudonym and shift, so extracts are only comparable when
they were built with the same key. The key's fingerprint (not the key) goes into extract
metadata so this is checkable.
"""

from __future__ import annotations

import hashlib
from typing import Any

MIN_KEY_LENGTH = 32


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
            cur.execute(
                "insert into ops_secret.deid_key (key_id, key, fingerprint) values (%s, %s, %s)"
                " on conflict (key_id) do update set key = excluded.key,"
                " fingerprint = excluded.fingerprint, installed_at = now()"
                " where ops_secret.deid_key.fingerprint <> excluded.fingerprint",
                (key_id, key, fp),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fp
