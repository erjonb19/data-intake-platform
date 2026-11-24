"""24.35 (the approvals gateway's half): an error the gateway doesn't explain goes back
as a generic message with an id, and the full error only to the server's log."""

from __future__ import annotations

import hashlib
import logging
import re

import psycopg
from fastapi.testclient import TestClient

from dip.agents.gateway import create_app

HOST = "approvals.test"
AUTH = {"Authorization": "Bearer t"}
SECRET_DETAIL = "password authentication failed for user warehouse_owner"


def client(connect) -> TestClient:
    app = create_app(connect, [HOST], {"owner": hashlib.sha256(b"t").hexdigest()})
    return TestClient(app, base_url=f"http://{HOST}", raise_server_exceptions=False)


def test_an_unexpected_error_is_generic_with_an_id_and_logged_in_full(caplog):
    def broken():
        raise RuntimeError(SECRET_DETAIL)

    with caplog.at_level(logging.ERROR, logger="dip.approvals"):
        r = client(broken).get("/api/approvals", headers=AUTH)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert SECRET_DETAIL not in r.text and "Traceback" not in r.text
    m = re.fullmatch(r"internal error \(error ([0-9a-f]{12})\)", detail)
    assert m, detail
    assert any(m.group(1) in rec.getMessage() and rec.exc_info for rec in caplog.records)


class _NoTable:
    def cursor(self):
        raise psycopg.errors.UndefinedTable('relation "ops.approvals" does not exist')

    def close(self):
        pass


def test_warehouse_not_ready_names_no_table(caplog):
    with caplog.at_level(logging.WARNING, logger="dip.approvals"):
        r = client(_NoTable).get("/api/approvals", headers=AUTH)
    assert r.status_code == 503
    assert "ops.approvals" not in r.text
    m = re.fullmatch(r"warehouse not ready \(error ([0-9a-f]{12})\)", r.json()["detail"])
    assert m, r.text
    assert any(m.group(1) in rec.getMessage() and "ops.approvals" in rec.getMessage()
               for rec in caplog.records)  # fmt: skip
