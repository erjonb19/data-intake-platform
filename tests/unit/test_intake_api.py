"""Intake API (Stage 2): authentication, authorization, limits, and what lands where.
Keys and files are invented."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dip import BUCKET_LANDING, CHANNEL_KEY
from dip.contracts import Contract, load_contracts
from dip.intake import process_landing
from dip.intake_api import create_app, load_clients
from dip.storage import LocalStore
from tests.unit.test_intake import FakeWarehouse

FEED = "healthcare.eligibility"
GOOD = (
    Path(__file__).resolve().parents[1] / "fixtures/eligibility/good/elig_good.csv"
).read_bytes()
KEYS = {"healthcare": "hc-test-key-0123456789", "finance": "fin-test-key-0123456789"}


def clients():
    return load_clients(
        json.dumps(
            {
                "healthcare": {
                    "sha256": hashlib.sha256(KEYS["healthcare"].encode()).hexdigest(),
                    "feeds": ["healthcare.*"],
                },
                "finance": {
                    "sha256": hashlib.sha256(KEYS["finance"].encode()).hexdigest(),
                    "feeds": ["finance.*"],
                },
            }
        )
    )


def contracts(channels=("minio", "sftp", "api")):
    c = load_contracts()
    e = c[FEED]
    c[FEED] = Contract(
        e.feed,
        e.version,
        {**e.spec, "source": {**e.spec["source"], "channels": list(channels)}},
        e.path,
    )
    return c


@pytest.fixture
def setup(tmp_path):
    store = LocalStore(tmp_path)
    return store, TestClient(create_app(store, contracts(), clients(), max_bytes=10_000))


def post(api, feed=FEED, filename="elig_20261022.csv", data=GOOD, key=KEYS["healthcare"]):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return api.post(f"/v1/feeds/{feed}/files", params={"filename": filename}, content=data,
                    headers=headers)  # fmt: skip


def test_upload_lands_with_its_channel_and_client(setup):
    store, api = setup
    r = post(api)
    assert r.status_code == 202
    assert r.json() == {
        "feed": FEED,
        "key": f"{FEED}/elig_20261022.csv",
        "sha256": hashlib.sha256(GOOD).hexdigest(),
        "bytes": len(GOOD),
    }
    assert store.get(BUCKET_LANDING, f"{FEED}/elig_20261022.csv") == GOOD
    assert store.metadata(BUCKET_LANDING, f"{FEED}/elig_20261022.csv") == {
        CHANNEL_KEY: "api",
        "dip-api-client": "healthcare",
    }


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"key": None}, 401),
        ({"key": "wrong-key"}, 401),
        ({"key": KEYS["finance"]}, 403),  # another pack's feed
        ({"feed": "healthcare.nothing"}, 404),
        ({"filename": ""}, 422),
        ({"filename": "../escape.csv"}, 422),
        ({"filename": "sub/dir.csv"}, 422),
        ({"filename": ".hidden"}, 422),
        ({"data": b"x" * 10_001}, 413),
    ],
)
def test_refusals(setup, kwargs, status):
    store, api = setup
    assert post(api, **kwargs).status_code == status
    assert store.list(BUCKET_LANDING) == []


def test_a_feed_without_the_api_channel_is_refused(tmp_path):
    store = LocalStore(tmp_path)
    api = TestClient(create_app(store, contracts(("minio",)), clients()))
    r = post(api)
    assert r.status_code == 403 and "api channel" in r.json()["detail"]


def test_an_unprocessed_file_is_not_overwritten(setup):
    store, api = setup
    assert post(api).status_code == 202
    assert post(api, data=GOOD + b"\n").status_code == 409
    assert store.get(BUCKET_LANDING, f"{FEED}/elig_20261022.csv") == GOOD


def test_healthz(setup):
    assert setup[1].get("/healthz").json() == {"status": "ok"}


def test_uploaded_file_goes_through_the_intake_as_api(setup):
    store, api = setup
    assert post(api, filename="elig_20261021.csv").status_code == 202
    wh = FakeWarehouse()
    [o] = process_landing(store, wh, contracts(), datetime(2026, 10, 22, tzinfo=UTC))
    assert (o.status, o.accepted) == ("loaded", 6)
    assert [i.channel for i in wh.intake] == ["api"]


def test_another_packs_feed_names_cannot_be_probed(setup):
    """Review finding: a client must not tell 'no such feed' from 'not yours' outside its
    own patterns, so authorization comes before the 404."""
    _, api = setup
    assert post(api, feed="healthcare.nothing", key=KEYS["finance"]).status_code == 403
    assert post(api, feed="healthcare.nothing").status_code == 404  # its own pack: 404
