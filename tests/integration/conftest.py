"""Postgres fixtures. DIP_TEST_DSN must point at a throwaway database: the schemas the
pipeline owns are dropped and recreated."""

from __future__ import annotations

import os

import pytest

from dip.warehouse import Warehouse

DSN = os.environ.get("DIP_TEST_DSN")
OWNED_SCHEMAS = ("bronze", "ops", "silver", "omop", "vocab", "dbt")


@pytest.fixture
def warehouse():
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
    yield wh
    conn.close()


def scalar(wh, sql, params=None):
    with wh.conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def rows(wh, sql, params=None):
    with wh.conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
