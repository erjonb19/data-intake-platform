"""24.33: the approvals page's security headers, on every response."""

from __future__ import annotations

import base64
import hashlib
import re
from importlib.resources import files

from fastapi.testclient import TestClient

from dip.agents.gateway import create_app

HOST = "approvals.test"


def client() -> TestClient:
    def no_db():
        raise AssertionError("these requests never reach the warehouse")

    app = create_app(no_db, [HOST], {"owner": hashlib.sha256(b"t").hexdigest()})
    return TestClient(app, base_url=f"http://{HOST}")


def test_the_page_carries_a_csp_that_allows_only_its_own_inline_code():
    r = client().get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    html = files("dip.agents").joinpath("approvals.html").read_text(encoding="utf-8")
    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        assert html.count(f"<{tag}>") == 1, f"one inline <{tag}> (its hash is in the CSP)"
        body = html.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        h = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"{directive} 'sha256-{h}'" in csp
    # an inline style attribute or event handler would be blocked by this CSP
    assert not re.search(r"<[^>]+\s(style|on[a-z]+)=", html)
    for name, value in (("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer"),
                        ("x-frame-options", "DENY")):  # fmt: skip
        assert r.headers[name] == value


def test_api_answers_are_never_cached_even_refusals():
    r = client().get("/api/approvals")
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_refused_host_still_gets_the_headers():
    r = TestClient(client().app, base_url="http://evil.test").get("/")
    assert r.status_code == 400
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
