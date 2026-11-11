"""Versioned warehouse migrations (14.3).

The warehouse's own schema (the ops tables, bronze, the grants each login gets) is a series
of numbered SQL files in dip/sql/migrations/, `NNNN_name.sql`, applied in order and once
each. `ops.schema_migration` records every one applied, with the SHA-256 of its text.

`migrate` runs at deploy, before the services start (the `warehouse-init` Job in
infra/terraform/platform_check.tf, `python -m dip migrate` by hand), under an advisory
lock, in one transaction: either every pending migration applies or none does. A
migration already applied whose file has changed since is refused (`MigrationChanged`):
a change to the schema is a new file, never an edit.

`Warehouse.init` (every intake task, the agent chain, the tests) calls `ensure`, which
reads the table and does nothing when the warehouse is current, so tasks no longer
re-apply the whole DDL, whose exclusive locks deadlocked with the MCP server's queries
(#115's e2e, 2026-11-09). Only a warehouse that is behind (a laptop, a test database, or
before the Job has run) gets the pending migrations applied there.

0001 is the schema as it stood before this (dip/sql/warehouse.sql, idempotent), so a
warehouse from an earlier release takes it without change and records it as applied.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

LOCK = 724163002  # 0001 itself takes 724163001 inside the same transaction
NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

TABLE = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.schema_migration (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    sha256      text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationChanged(RuntimeError):
    """An applied migration's file no longer matches what was applied."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def migrations() -> list[Migration]:
    """Every migration file, in version order. Versions start at 1 with no gaps."""
    root = files("dip").joinpath("sql/migrations")
    found = []
    for f in root.iterdir():
        if not f.name.endswith(".sql"):
            continue
        m = NAME.match(f.name)
        if not m:
            raise ValueError(f"migration file {f.name!r} is not named NNNN_name.sql")
        # Universal newlines: a CRLF checkout hashes the same as an LF one.
        text = f.read_text("utf-8").replace("\r\n", "\n")
        found.append(Migration(int(m.group(1)), f.name, text))
    found.sort(key=lambda m: m.version)
    want = list(range(1, len(found) + 1))
    if [m.version for m in found] != want:
        raise ValueError(f"migration versions {[m.version for m in found]} are not 1..N")
    return found


def applied(conn: Any) -> dict[int, str]:
    """version -> sha256 of every applied migration ({} before the table exists)."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('ops.schema_migration') is not null")
        if not cur.fetchone()[0]:
            return {}
        cur.execute("select version, sha256 from ops.schema_migration")
        return {v: s for v, s in cur.fetchall()}


def _check(done: dict[int, str], all_: list[Migration]) -> list[Migration]:
    """The pending migrations; raises when an applied one has changed or is unknown."""
    by_version = {m.version: m for m in all_}
    for v, sha in done.items():
        # A version this code does not know: the warehouse is ahead of this image (a rollback
        # to an older release, 13.2). Not an error: what this image needs is applied.
        if v not in by_version:
            continue
        if by_version[v].sha256 != sha:
            raise MigrationChanged(
                f"migration {by_version[v].name} changed after it was applied; "
                "add a new migration instead of editing it"
            )
    return [m for m in all_ if m.version not in done]


def pending(conn: Any) -> list[Migration]:
    return _check(applied(conn), migrations())


def migrate(conn: Any) -> list[str]:
    """Apply every pending migration in one transaction; returns their names."""
    all_ = migrations()
    names = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            cur.execute(TABLE)
            # Read again under the lock: another process may have just applied them.
            for m in _check(applied(conn), all_):
                cur.execute(m.sql)
                cur.execute(
                    "insert into ops.schema_migration (version, name, sha256) values (%s, %s, %s)",
                    (m.version, m.name, m.sha256),
                )
                names.append(m.name)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return names


def ensure(conn: Any) -> list[str]:
    """Nothing when the warehouse is current (one read, no DDL, no lock); otherwise
    `migrate`. Raises MigrationChanged as migrate does."""
    try:
        if not pending(conn):
            conn.commit()  # end the read's transaction
            return []
    except BaseException:  # MigrationChanged, or the read failed: leave no aborted transaction
        conn.rollback()
        raise
    conn.rollback()
    return migrate(conn)


GRANT_FN = re.compile(r"^grant_[a-z_]+$")


def reapply_grants(conn: Any) -> list[str]:
    """Call every `ops.grant_*()` function the migrations defined; returns their names.

    Each one grants a login what it may use, and does nothing when its login does not exist
    yet. The `warehouse-init` Job runs this after `migrate` on every deploy, so a login the
    roles Job created after the migration that defines its grants still gets them, as it
    did when every task re-applied the whole schema. Grants only: no DDL, no table lock."""
    with conn.cursor() as cur:
        cur.execute(
            "select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'ops' and p.proname like 'grant\_%' and p.pronargs = 0"
            " order by 1"
        )
        names = [r[0] for r in cur.fetchall() if GRANT_FN.match(r[0])]
        for name in names:
            cur.execute(f"select ops.{name}()")  # name checked against GRANT_FN
    conn.commit()
    return names
