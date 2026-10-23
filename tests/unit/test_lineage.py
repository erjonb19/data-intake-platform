from __future__ import annotations

import json

from dip import lineage
from dip.intake import Outcome

DSN = "postgresql://u:p@db.example:5433/warehouse"


def read(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_disabled_emitter_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("DIP_LINEAGE_FILE", raising=False)
    e = lineage.Emitter()
    assert not e.enabled
    lineage.intake_event(e, DSN, "f.x", Outcome("f.x/a.json", "abc", "loaded", accepted=1))


def test_pg_dataset_naming_matches_openlineage_convention():
    ds = lineage.pg_dataset(DSN, "omop", "person")
    assert ds["namespace"] == "postgres://db.example:5433"
    assert ds["name"] == "warehouse.omop.person"


def test_intake_event_shape(tmp_path):
    out = tmp_path / "ol.ndjson"
    e = lineage.Emitter(url="", file=out)
    o = Outcome(
        "feed.a/a.json",
        "abc",
        "loaded_with_rejections",
        accepted=8,
        rejected=1,
        reason_codes=("FHIR.MISSING_FIELD:Observation.status",),
        raw_key="feed.a/2026-01-01/abc.json",
        quarantine_keys=("feed.a/2026-01-01/abc.rejected.ndjson",),
    )
    lineage.intake_event(e, DSN, "feed.a", o)
    start, complete = read(out)
    assert (start["eventType"], complete["eventType"]) == ("START", "COMPLETE")
    assert start["run"]["runId"] == complete["run"]["runId"]
    for key in ("eventTime", "run", "job", "inputs", "outputs", "producer", "schemaURL"):
        assert key in complete
    assert complete["job"] == {"namespace": "dip", "name": "intake.feed.a", "facets": {}}
    assert complete["inputs"][0] == {
        "namespace": "s3://landing",
        "name": "feed.a/a.json",
        "facets": {},
    }
    names = {(d["namespace"], d["name"]) for d in complete["outputs"]}
    assert names == {
        ("s3://raw", "feed.a/2026-01-01/abc.json"),
        ("postgres://db.example:5433", "warehouse.bronze.fhir_resource"),
        ("s3://quarantine", "feed.a/2026-01-01/abc.rejected.ndjson"),
    }
    facet = complete["run"]["facets"]["dip_intake"]
    assert facet["reason_codes"] == ["FHIR.MISSING_FIELD:Observation.status"]
    assert facet["_producer"] and facet["_schemaURL"]


def test_dbt_events_from_artifacts(tmp_path):
    manifest = {
        "nodes": {
            "model.dip.person": {
                "resource_type": "model",
                "name": "person",
                "schema": "omop",
                "alias": "person",
                "description": "One row per patient.",
                "columns": {"person_id": {"name": "person_id", "description": "Key."}},
                "depends_on": {"nodes": ["model.dip.fhir_patient", "seed.dip.fhir_concept_map"]},
            },
            "model.dip.fhir_patient": {
                "resource_type": "model",
                "name": "fhir_patient",
                "schema": "silver",
                "alias": "fhir_patient",
                "columns": {},
                "depends_on": {"nodes": []},
            },
            "seed.dip.fhir_concept_map": {
                "resource_type": "seed",
                "name": "fhir_concept_map",
                "schema": "vocab",
                "alias": "fhir_concept_map",
                "columns": {},
                "depends_on": {"nodes": []},
            },
            "test.dip.unique_person_person_id": {
                "resource_type": "test",
                "name": "unique_person_person_id",
                "attached_node": "model.dip.person",
                "column_name": "person_id",
            },
        },
        "sources": {},
    }
    results = {
        "results": [
            {"unique_id": "model.dip.person", "status": "success"},
            {"unique_id": "test.dip.unique_person_person_id", "status": "pass"},
            {"unique_id": "model.dip.fhir_patient", "status": "error"},
        ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "run_results.json").write_text(json.dumps(results))
    out = tmp_path / "ol.ndjson"
    assert lineage.dbt_events(lineage.Emitter(url="", file=out), DSN, tmp_path) == 2
    complete = read(out)[1]
    assert complete["job"]["name"] == "dbt.person"
    assert {d["name"] for d in complete["inputs"]} == {
        "warehouse.silver.fhir_patient",
        "warehouse.vocab.fhir_concept_map",
    }
    facets = complete["outputs"][0]["facets"]
    assert facets["dataQualityAssertions"]["assertions"] == [
        {"assertion": "unique_person_person_id", "success": True, "column": "person_id"}
    ]
    assert facets["documentation"]["description"] == "One row per patient."
    assert facets["schema"]["fields"] == [{"name": "person_id", "description": "Key."}]


def test_an_unreachable_lineage_server_never_fails_the_run(capsys):
    # Port 9 (discard) on localhost: nothing listens, the connection is refused at once.
    e = lineage.Emitter(url="http://127.0.0.1:9", file="")
    e.job("dip.test", [], [])
    assert e.failures == 2  # START and COMPLETE
    assert "refused an event" in capsys.readouterr().out


def test_a_file_feed_points_at_the_table_its_dbt_sources_read(tmp_path):
    out = tmp_path / "events.ndjson"
    e = lineage.Emitter(url="", file=out)
    o = Outcome("finance.vendor_invoice/a.xlsx", "abc", "loaded", accepted=3)
    lineage.intake_event(e, DSN, "finance.vendor_invoice", o, bronze="csv_record")
    done = [ev for ev in read(out) if ev["eventType"] == "COMPLETE"][0]
    assert "warehouse.bronze.csv_record" in [d["name"] for d in done["outputs"]]
