"""24.37: every route on the approvals page is either public on purpose or refuses a
request without a valid token, before it opens a database connection. The routes come
from the app itself, so a new /api route is covered the day it is added."""

from __future__ import annotations

import hashlib
import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dip.agents.gateway import create_app

HOST = "approvals.test"
PUBLIC = {"/", "/healthz"}  # the page itself (it holds no data) and the probe
BAD = ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"},
       {"Authorization": "Basic dDp0"}, {"Authorization": "t"})  # fmt: skip


def app_and_calls():
    calls = []

    def connect():
        calls.append(1)
        raise AssertionError("a refused request opened a database connection")

    app = create_app(connect, [HOST], {"owner": hashlib.sha256(b"t").hexdigest()})
    return app, calls


def test_every_route_is_public_on_purpose_or_under_api():
    app, _ = app_and_calls()
    # a Mount or a plain Starlette Route would skip FastAPI's dependencies, and this test
    assert all(isinstance(r, APIRoute) for r in app.routes), [type(r) for r in app.routes]
    paths = {r.path for r in app.routes}
    assert paths - PUBLIC, "no API routes found"
    for path in paths - PUBLIC:
        assert path.startswith("/api/"), f"{path}: not under /api, so not behind the token"


def test_every_api_route_and_method_refuses_without_a_valid_token():
    app, calls = app_and_calls()
    client = TestClient(app, base_url=f"http://{HOST}", raise_server_exceptions=False)
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC:
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in route.methods:
            for headers in BAD:
                r = client.request(method, path, headers={"X-Reviewer": "owner", **headers},
                                   json={"decision": "approve"})  # fmt: skip
                assert r.status_code == 401, (method, route.path, headers, r.status_code)
                assert r.headers.get("www-authenticate") == "Bearer"
                checked += 1
    assert checked >= 5 * len(BAD)  # 5 routes today: list, show, decide, audit, trace
    assert calls == []
