"""R5: versioned research extracts are reproducible byte for byte.

Runs the whole pipeline twice from scratch (fresh warehouse, same raw files, different
arrival times): intake -> vocabulary -> dbt build -> extract. The two extracts must be
identical. Uses the fixture patient plus DIP_SYNTHEA_DIR when set.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, extract, vocab
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import good_bundle
from tests.integration.conftest import DSN, _fresh_warehouse

FEED = "healthcare.fhir_patient_bundle"
CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
KEY = "ci-test-key-0123456789abcdef0123456789abcdef"
COMMIT = "0" * 40  # fixed so the test does not depend on the checkout


def _raw_files() -> list[tuple[str, bytes]]:
    files = [("fixture.json", json.dumps(good_bundle()).encode())]
    if synthea := os.environ.get("DIP_SYNTHEA_DIR"):
        files += [(f.name, f.read_bytes()) for f in sorted(Path(synthea).glob("*.json"))]
    return files


def _pipeline(store_dir: Path, out: Path, received_at: datetime, version="1.0.0"):
    wh = _fresh_warehouse()
    store = LocalStore(store_dir)
    for name, data in _raw_files():
        store.put(BUCKET_LANDING, f"{FEED}/{name}", data)
    process_landing(store, wh, load_contracts(), received_at)
    vocab.load(wh.conn, vocab.fetch(CACHE))
    install_key(wh.conn, KEY)
    assert run_dbt(DSN, "build", check=False).returncode == 0
    return wh, extract.build(wh.conn, out, version, git_commit=COMMIT)


@pytest.fixture(scope="module")
def two_builds(tmp_path_factory):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    a_out, b_out = tmp_path_factory.mktemp("a"), tmp_path_factory.mktemp("b")
    wh_a, a = _pipeline(tmp_path_factory.mktemp("sa"), a_out, datetime(2026, 1, 5, tzinfo=UTC))
    wh_a.conn.close()
    wh_b, b = _pipeline(tmp_path_factory.mktemp("sb"), b_out, datetime(2026, 3, 9, tzinfo=UTC))
    yield wh_b, a, b
    wh_b.conn.close()


def test_two_independent_rebuilds_are_byte_identical(two_builds):
    _, a, b = two_builds
    assert extract.diff(a.path, b.path) == []
    assert a.manifest_sha256 == b.manifest_sha256
    print(f"extract manifest sha256: {a.manifest_sha256}")


def test_manifest_matches_files(two_builds):
    _, a, _ = two_builds
    lines = (a.path / extract.MANIFEST).read_text("utf-8").splitlines()
    assert len(lines) == len(extract.TABLES) + 1  # tables + datapackage.json
    for line in lines:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((a.path / name).read_bytes()).hexdigest() == digest


def test_datapackage_descriptor(two_builds):
    _, a, _ = two_builds
    pkg = json.loads((a.path / extract.DESCRIPTOR).read_text("utf-8"))
    assert pkg["version"] == "1.0.0"
    assert {r["name"] for r in pkg["resources"]} == set(extract.TABLES)
    assert pkg["provenance"]["vocabulary"]["sha256"] == vocab.EUNOMIA_SHA256
    assert pkg["provenance"]["code_git_commit"] == COMMIT
    assert len(pkg["provenance"]["deid_key_fingerprint"]) == 16
    assert KEY not in json.dumps(pkg)
    person = next(r for r in pkg["resources"] if r["name"] == "person")
    assert person["schema"]["primaryKey"] == ["person_id"]
    assert "person_source_value" in [f["name"] for f in person["schema"]["fields"]]
    assert "no real patient data" in pkg["description"]


def test_published_version_is_immutable(two_builds):
    wh, _, b = two_builds
    again = extract.build(wh.conn, b.path.parent, "1.0.0", git_commit=COMMIT)
    assert again.reproduced_existing
    with wh.conn.cursor() as cur:
        cur.execute(
            "delete from omop_shareable.measurement where measurement_id ="
            " (select min(measurement_id) from omop_shareable.measurement)"
        )
    wh.conn.commit()
    with pytest.raises(extract.ExtractExistsError, match="immutable"):
        extract.build(wh.conn, b.path.parent, "1.0.0", git_commit=COMMIT)
