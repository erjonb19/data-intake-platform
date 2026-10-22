"""Postgres fixtures. DIP_TEST_DSN must point at a throwaway database: the schemas the
pipeline owns are dropped and recreated."""

from __future__ import annotations

import os

import pytest

from dip.warehouse import Warehouse

DSN = os.environ.get("DIP_TEST_DSN")
OWNED_SCHEMAS = (
    "bronze",
    "ops",
    "ops_secret",
    "silver",
    "gold",
    "omop",
    "deid",
    "omop_shareable",
    "vocab",
    "dbt",
)


def _fresh_warehouse():
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    import psycopg

    conn = psycopg.connect(DSN)
    with conn.cursor() as cur:
        for schema in OWNED_SCHEMAS:
            cur.execute(f"drop schema if exists {schema} cascade")
    conn.commit()
    wh = Warehouse(conn)
    wh.init()
    wh.init()  # idempotent
    return wh


@pytest.fixture
def warehouse():
    wh = _fresh_warehouse()
    yield wh
    wh.conn.close()


@pytest.fixture(scope="module")
def module_warehouse():
    """One warehouse for a whole module, for expensive setups such as a dbt build."""
    wh = _fresh_warehouse()
    yield wh
    wh.conn.close()


def rows(wh, sql, params=None):
    # Commit after reading: an open read transaction holds locks that would block dbt
    # from replacing the tables in a later test.
    with wh.conn.cursor() as cur:
        cur.execute(sql, params)
        result = cur.fetchall()
    wh.conn.commit()
    return result


def scalar(wh, sql, params=None):
    return rows(wh, sql, params)[0][0]
