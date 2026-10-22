from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from dip import BUCKET_LANDING, BUCKET_QUARANTINE, BUCKET_RAW
from dip.contracts import ContractError, load_contract, load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import bad_missing_birthdate, good_bundle

FEED = "healthcare.fhir_patient_bundle"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class FakeWarehouse:
    def __init__(self):
        self.intake, self.quarantine, self.bronze = [], [], []

    def already_processed(self, feed, sha):
        return any(i.feed == feed and i.file_sha256 == sha for i in self.intake)

    def record_outcome(self, intake, quarantine, resources, patient_id, loaded_at, csv_rows=()):
        self.intake.append(intake)
        self.quarantine.extend(quarantine)
        self.bronze.extend((patient_id, r) for r in resources)
        self.csv_rows = getattr(self, "csv_rows", []) + list(csv_rows)


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path)


def land(store, name, doc):
    data = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    store.put(BUCKET_LANDING, f"{FEED}/{name}", data)


def test_good_and_bad_files(store):
    wh = FakeWarehouse()
    land(store, "good.json", good_bundle())
    land(store, "bad.json", bad_missing_birthdate())
    out = {
        o.source_key.split("/")[-1]: o for o in process_landing(store, wh, load_contracts(), NOW)
    }

    assert out["good.json"].status == "loaded"
    assert out["good.json"].accepted == 9
    assert out["bad.json"].status == "quarantined"
    assert out["bad.json"].reason_codes == (
        "FHIR.PATIENT_INVALID:FHIR.MISSING_FIELD:Patient.birthDate",
    )
    # Bad file: nothing in bronze, the file and a reason document are in quarantine.
    assert len(wh.bronze) == 9
    sha = out["bad.json"].file_sha256
    assert store.list(BUCKET_QUARANTINE) == sorted(
        [f"{FEED}/2026-10-21/{sha}.json", f"{FEED}/2026-10-21/{sha}.reason.json"]
    )
    # Both files keep an immutable raw copy, and landing is empty.
    assert len(store.list(BUCKET_RAW, f"{FEED}/2026-10-21/")) == 2
    assert store.list(BUCKET_LANDING) == []


def test_rerun_of_same_file_is_a_duplicate(store):
    wh = FakeWarehouse()
    contracts = load_contracts()
    land(store, "a.json", good_bundle())
    process_landing(store, wh, contracts, NOW)
    land(store, "a-again.json", good_bundle())
    [again] = process_landing(store, wh, contracts, NOW)
    assert again.status == "duplicate"
    assert len(wh.bronze) == 9


def test_record_rejections_go_to_ndjson(store, monkeypatch):
    wh = FakeWarehouse()
    contract = load_contracts()[FEED]
    monkeypatch.setitem(contract.spec, "max_bad_row_ratio", 0.5)
    b = good_bundle()
    b["entry"][5]["resource"].pop("status")
    land(store, "partial.json", b)
    [o] = process_landing(store, wh, {FEED: contract}, NOW)
    assert o.status == "loaded_with_rejections"
    [q] = wh.quarantine
    assert q.level == "record"
    assert q.record_ref == "Observation/obs-0001#entry=5"
    line = json.loads(store.get(BUCKET_QUARANTINE, q.quarantine_key))
    assert line["reason_code"] == "FHIR.MISSING_FIELD:Observation.status"


def test_filename_pattern(store):
    wh = FakeWarehouse()
    land(store, "notes.txt", b"hello")
    [o] = process_landing(store, wh, load_contracts(), NOW)
    assert o.reason_codes == ("FILE.NAME_PATTERN",)


def test_unknown_feed_fails_loudly(store):
    store.put(BUCKET_LANDING, "nobody.feed/x.json", b"{}")
    with pytest.raises(KeyError, match="no contract"):
        process_landing(store, FakeWarehouse(), load_contracts(), NOW)


def test_local_store_rejects_path_escape(store):
    with pytest.raises(ValueError):
        store.put(BUCKET_RAW, "../landing/x", b"")


def test_invalid_contract_is_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("feed: Bad Name\nversion: 1\n")
    with pytest.raises(ContractError):
        load_contract(p)
