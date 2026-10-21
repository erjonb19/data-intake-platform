"""R6: OpenLineage events connect a raw FHIR file all the way to the research extract.

Runs intake -> dbt build -> extract with a file transport, then walks the lineage graph
(input dataset -> output dataset edges from COMPLETE events).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, extract, lineage, vocab
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import bad_missing_birthdate, good_bundle
from tests.integration.conftest import DSN, _fresh_warehouse

FEED = "healthcare.fhir_patient_bundle"
CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
KEY = "ci-test-key-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def events(tmp_path_factory):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    root = tmp_path_factory.mktemp("lineage")
    log = root / "openlineage.ndjson"
    os.environ["DIP_LINEAGE_FILE"] = str(log)
    try:
        wh = _fresh_warehouse()
        store = LocalStore(root / "store")
        store.put(BUCKET_LANDING, f"{FEED}/good.json", json.dumps(good_bundle()).encode())
        store.put(BUCKET_LANDING, f"{FEED}/bad.json", json.dumps(bad_missing_birthdate()).encode())
        emitter = lineage.Emitter()
        for o in process_landing(store, wh, load_contracts(), datetime.now(UTC)):
            lineage.intake_event(emitter, DSN, FEED, o)
        vocab.load(wh.conn, vocab.fetch(CACHE))
        install_key(wh.conn, KEY)
        assert run_dbt(DSN, "build", check=False).returncode == 0
        result = extract.build(wh.conn, root / "extracts", "1.0.0", git_commit="0" * 40)
        lineage.extract_event(emitter, DSN, result, list(extract.TABLES))
        wh.conn.close()
    finally:
        os.environ.pop("DIP_LINEAGE_FILE", None)
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


def _key(d):
    return f"{d['namespace']}/{d['name']}"


def test_events_are_well_formed(events):
    assert events
    for e in events:
        assert e["eventType"] in ("START", "COMPLETE")
        assert e["schemaURL"].startswith("https://openlineage.io/spec/")
        assert e["job"]["namespace"] == "dip"


def test_lineage_reaches_extract_from_landing_file(events):
    edges = defaultdict(set)
    for e in events:
        if e["eventType"] == "COMPLETE":
            for i in e["inputs"]:
                for o in e["outputs"]:
                    edges[_key(i)].add(_key(o))
    start = f"s3://landing/{FEED}/good.json"
    target = "file/extracts/1.0.0/data/measurement.csv"
    seen, frontier, path_found = {start}, [start], False
    while frontier:
        node = frontier.pop()
        if node == target:
            path_found = True
            break
        for nxt in edges[node] - seen:
            seen.add(nxt)
            frontier.append(nxt)
    assert path_found, f"no lineage path from {start} to {target}"
    ns, db = lineage.pg_namespace(DSN)
    for table in ("bronze.fhir_resource", "omop.measurement", "omop_shareable.measurement"):
        assert f"{ns}/{db}.{table}" in seen


def test_bad_file_lineage_points_to_quarantine(events):
    bad = [
        e
        for e in events
        if e["eventType"] == "COMPLETE"
        and e["inputs"]
        and e["inputs"][0]["name"].endswith("bad.json")
    ]
    assert len(bad) == 1
    outputs = {o["namespace"] for o in bad[0]["outputs"]}
    assert outputs == {"s3://raw", "s3://quarantine"}  # never bronze
    assert bad[0]["run"]["facets"]["dip_intake"]["status"] == "quarantined"


def test_dbt_events_carry_test_results(events):
    measurement = next(
        e for e in events if e["eventType"] == "COMPLETE" and e["job"]["name"] == "dbt.measurement"
    )
    assertions = measurement["outputs"][0]["facets"]["dataQualityAssertions"]["assertions"]
    names = {a["assertion"] for a in assertions}
    assert "plausible_value_hba1c" in names
    assert all(a["success"] for a in assertions if a["assertion"] == "plausible_value_hba1c")
