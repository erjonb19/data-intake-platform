"""OpenLineage events for the research pipeline (R6).

Every layer reports what it read and wrote, using OpenLineage dataset naming so the
pieces join into one graph in Marquez:

  intake   s3://landing/<file>  ->  s3://raw/<copy>, postgres bronze.fhir_resource, quarantine
  dbt      one job per model: its upstream models/sources -> the model's table, with the
           model's columns, description and test results (dataQualityAssertions)
  extract  postgres omop_shareable.* -> file extracts/<version>/data/<table>.csv

Transport comes from the environment: OPENLINEAGE_URL (HTTP: Marquez, in the
observability profile; best effort, so an unreachable server never fails a run) and/or
DIP_LINEAGE_FILE (append NDJSON, used by tests). With neither set, emitting is a no-op.
No dependencies: events are plain JSON per the OpenLineage spec.

dbt events are built from dbt's own artifacts (manifest.json, run_results.json) rather
than with openlineage-dbt, whose openlineage-sql dependency has no wheel for every
platform (it needs a Rust toolchain on Windows).
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PRODUCER = "https://github.com/erjonb19/data-intake-platform"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/RunEvent"
FACETS = "https://openlineage.io/spec/facets"
DIP_FACET_SCHEMA = f"{PRODUCER}/blob/main/docs/research/README.md#r6-lineage"
JOB_NAMESPACE = os.environ.get("DIP_LINEAGE_NAMESPACE", "dip")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def facet(schema: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": schema, **fields}


def dataset(namespace: str, name: str, facets: dict | None = None) -> dict[str, Any]:
    return {"namespace": namespace, "name": name, "facets": facets or {}}


def pg_namespace(dsn: str) -> tuple[str, str]:
    """OpenLineage namespace and database name for a Postgres DSN."""
    u = urlsplit(dsn)
    return f"postgres://{u.hostname or 'localhost'}:{u.port or 5432}", u.path.lstrip("/")


def pg_dataset(dsn: str, schema: str, table: str, facets: dict | None = None) -> dict:
    ns, db = pg_namespace(dsn)
    return dataset(ns, f"{db}.{schema}.{table}", facets)


def s3_dataset(bucket: str, key: str) -> dict:
    return dataset(f"s3://{bucket}", key)


def run_event(
    event_type: str,
    run_id: str,
    job: str,
    inputs: list[dict] = (),
    outputs: list[dict] = (),
    run_facets: dict | None = None,
    job_facets: dict | None = None,
    event_time: str | None = None,
) -> dict[str, Any]:
    return {
        "eventType": event_type,
        "eventTime": event_time or _now(),
        "run": {"runId": run_id, "facets": run_facets or {}},
        "job": {"namespace": JOB_NAMESPACE, "name": job, "facets": job_facets or {}},
        "inputs": list(inputs),
        "outputs": list(outputs),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
    }


class Emitter:
    def __init__(self, url: str | None = None, file: str | Path | None = None):
        self.url = url if url is not None else os.environ.get("OPENLINEAGE_URL")
        file = file if file is not None else os.environ.get("DIP_LINEAGE_FILE")
        self.file = Path(file) if file else None
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url or self.file)

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, sort_keys=True)
        if self.file:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.url:
            req = urllib.request.Request(
                self.url.rstrip("/") + "/api/v1/lineage",
                data=line.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Best effort: lineage is observability, and the data it describes is already
            # committed, so an unreachable Marquez must not fail the run. Counted, and
            # printed into the task log, where the triage agent and a person can see it.
            try:
                urllib.request.urlopen(req, timeout=10).close()
            except (OSError, http.client.HTTPException) as exc:  # refused, timeouts, cut off
                self.failures += 1
                print(f"lineage: {self.url} refused an event ({type(exc).__name__}: {exc})")

    def job(self, job: str, inputs, outputs, run_facets=None, job_facets=None) -> str:
        """START then COMPLETE for a job that has already finished; returns the run id."""
        run_id = str(uuid.uuid4())
        self.emit(run_event("START", run_id, job, inputs, [], job_facets=job_facets))
        self.emit(run_event("COMPLETE", run_id, job, inputs, outputs, run_facets, job_facets))
        return run_id


def intake_event(
    emitter: Emitter, dsn: str, feed: str, outcome: Any, bronze: str = "fhir_resource"
) -> None:
    """Lineage for one processed file (an intake Outcome). `bronze` is the table its
    accepted records went to: fhir_resource for FHIR bundles, csv_record for the CSV,
    Excel and PDF feeds (the table their dbt sources read, so the path joins up)."""
    if not emitter.enabled or outcome.status == "duplicate":
        return
    outputs = [s3_dataset("raw", outcome.raw_key)]
    if outcome.accepted:
        outputs.append(pg_dataset(dsn, "bronze", bronze))
    outputs += [s3_dataset("quarantine", k) for k in outcome.quarantine_keys]
    emitter.job(
        f"intake.{feed}",
        [s3_dataset("landing", outcome.source_key)],
        outputs,
        run_facets={
            "dip_intake": facet(
                DIP_FACET_SCHEMA,
                status=outcome.status,
                accepted=outcome.accepted,
                rejected=outcome.rejected,
                reason_codes=list(outcome.reason_codes),
                file_sha256=outcome.file_sha256,
            )
        },
    )


def _relation(node: dict) -> tuple[str, str]:
    return node["schema"], node.get("alias") or node.get("identifier") or node["name"]


def dbt_events(emitter: Emitter, dsn: str, target_dir: Path) -> int:
    """Lineage for every model and seed that ran successfully; returns events emitted."""
    if not emitter.enabled:
        return 0
    manifest = json.loads((target_dir / "manifest.json").read_text("utf-8"))
    results = json.loads((target_dir / "run_results.json").read_text("utf-8"))["results"]
    nodes = {**manifest["nodes"], **manifest["sources"]}
    tests_by_node: dict[str, list[dict]] = {}
    for r in results:
        node = nodes.get(r["unique_id"], {})
        if node.get("resource_type") == "test" and node.get("attached_node"):
            tests_by_node.setdefault(node["attached_node"], []).append(
                {
                    "assertion": node["name"],
                    "success": r["status"] == "pass",
                    **({"column": node["column_name"]} if node.get("column_name") else {}),
                }
            )
    parent = str(uuid.uuid4())
    sent = 0
    for r in results:
        node = nodes.get(r["unique_id"], {})
        if node.get("resource_type") not in ("model", "seed") or r["status"] != "success":
            continue
        inputs = [
            pg_dataset(dsn, *_relation(nodes[d]))
            for d in node.get("depends_on", {}).get("nodes", [])
            if d in nodes and nodes[d].get("resource_type") in ("model", "seed", "source")
        ]
        out_facets: dict[str, Any] = {
            "schema": facet(
                f"{FACETS}/1-1-1/SchemaDatasetFacet.json",
                fields=[
                    {"name": c["name"], "description": c.get("description") or ""}
                    for c in node.get("columns", {}).values()
                ],
            ),
        }
        if node.get("description"):
            out_facets["documentation"] = facet(
                f"{FACETS}/1-0-1/DocumentationDatasetFacet.json",
                description=node["description"],
            )
        if r["unique_id"] in tests_by_node:
            out_facets["dataQualityAssertions"] = facet(
                f"{FACETS}/1-0-1/DataQualityAssertionsDatasetFacet.json",
                assertions=tests_by_node[r["unique_id"]],
            )
        emitter.job(
            f"dbt.{node['name']}",
            inputs,
            [pg_dataset(dsn, *_relation(node), facets=out_facets)],
            run_facets={
                "parent": facet(
                    f"{FACETS}/1-0-1/ParentRunFacet.json",
                    run={"runId": parent},
                    job={"namespace": JOB_NAMESPACE, "name": "dbt.build"},
                )
            },
        )
        sent += 2
    return sent


def extract_event(emitter: Emitter, dsn: str, result: Any, tables: list[str]) -> None:
    if not emitter.enabled:
        return
    version = result.path.name
    emitter.job(
        "extract.omop_shareable",
        [pg_dataset(dsn, "omop_shareable", t) for t in tables],
        [dataset("file", f"extracts/{version}/data/{t}.csv") for t in tables],
        run_facets={
            "dip_extract": facet(
                DIP_FACET_SCHEMA,
                version=version,
                manifest_sha256=result.manifest_sha256,
                reproduced_existing=result.reproduced_existing,
            )
        },
    )
