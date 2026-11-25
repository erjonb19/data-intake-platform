"""24.32: rate and concurrency limits on the HTTP services (dip/ratelimit.py, the intake API,
the approvals page, the MCP server)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dip.contracts import load_contracts
from dip.intake_api import create_app, load_clients
from dip.ratelimit import FailedAuthLimit, Window
from dip.storage import LocalStore

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_window_allows_its_limit_then_refuses_until_the_oldest_leaves():
    clock = Clock()
    w = Window(3, 60.0, clock)
    assert [w.hit("a") for _ in range(4)] == [True, True, True, False]
    assert w.hit("b")  # another key has its own count
    assert w.blocked("a") and 59 <= w.retry_after("a") <= 60
    clock.t += 61
    assert not w.blocked("a") and w.hit("a")


def test_the_window_never_stores_a_key_it_was_only_asked_about():
    w = Window(1, 60.0, Clock())
    for i in range(100):
        assert not w.blocked(f"addr-{i}")
    assert w._events == {}


def test_the_window_keeps_its_memory_bounded():
    clock = Clock()
    w = Window(5, 60.0, clock, max_keys=10)
    for i in range(50):
        w.hit(f"k{i}")
    assert len(w._events) <= 10


def _guarded(limit: int) -> TestClient:
    app = FastAPI()

    @app.get("/x")
    def x(ok: bool = False) -> dict:
        if not ok:
            raise HTTPException(401, "no")
        return {"ok": True}

    app.add_middleware(FailedAuthLimit, limit=limit)
    return TestClient(app)


def test_an_address_with_too_many_401s_is_refused_with_retry_after():
    c = _guarded(3)
    assert [c.get("/x").status_code for _ in range(3)] == [401, 401, 401]
    r = c.get("/x", params={"ok": True})  # even a good request, once blocked
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_successful_requests_are_never_counted():
    c = _guarded(2)
    for _ in range(20):
        assert c.get("/x", params={"ok": True}).status_code == 200


KEY = "test-key-not-a-secret"


def _intake(tmp: Path, **limits) -> TestClient:
    spec = {"sha256": hashlib.sha256(KEY.encode()).hexdigest(), "feeds": ["healthcare.*"]}
    clients = load_clients(json.dumps({"vendor": spec}))
    store = LocalStore(tmp)
    return TestClient(create_app(store, load_contracts(), clients, **limits))


def _send(c: TestClient, name: str, key: str = KEY):
    return c.post("/v1/feeds/healthcare.eligibility/files", params={"filename": name},
                  content=b"member_id\n", headers={"Authorization": f"Bearer {key}"})  # fmt: skip


def test_the_intake_api_limits_uploads_per_client_per_minute(tmp_path):
    c = _intake(tmp_path, rate_per_minute=2)
    codes = [_send(c, f"elig_{i}.csv").status_code for i in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429
    r = _send(c, "elig_x.csv")
    assert r.status_code == 429 and "retry-after" in r.headers
    assert 'dip_intake_api_refused_total{reason="rate_limited"} 2' in c.get("/metrics").text


def test_the_intake_api_slows_key_guessing_by_address(tmp_path):
    c = _intake(tmp_path, auth_fails_per_minute=3)
    assert [_send(c, "elig_1.csv", "wrong").status_code for _ in range(3)] == [401] * 3
    assert _send(c, "elig_1.csv").status_code == 429  # the good key too, from that address


def test_the_intake_api_refuses_an_upload_when_its_slots_are_full(tmp_path):
    r = _send(_intake(tmp_path, max_inflight=0), "elig_1.csv")
    assert r.status_code == 429 and r.headers["retry-after"] == "5"


def test_every_server_caps_its_open_connections():
    main = (ROOT / "dip" / "__main__.py").read_text("utf-8")
    assert main.count('limit_concurrency=int(os.environ.get("DIP_HTTP_MAX_CONCURRENCY"') == 2
    docker = (ROOT / "images" / "intake-api" / "Dockerfile").read_text("utf-8")
    assert '"--limit-concurrency", "16"' in docker


def test_the_approvals_page_and_mcp_server_carry_the_failed_auth_limit():
    for mod in ("dip/agents/gateway.py", "dip/agents/mcp_server.py"):
        src = (ROOT / mod).read_text("utf-8")
        assert "add_middleware(FailedAuthLimit, limit=auth_fails_per_minute)" in src, mod
