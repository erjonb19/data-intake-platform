"""Stage 3 (S3.3) end-to-end check of lineage in Marquez, after the file-feed cases.

For each path below, walks Marquez's lineage graph upstream from the gold table (the
graph's inEdges) and requires the feed's intake job among its ancestors, then requires a
run of that job that read the landed file. Marquez draws the graph from each job's latest
version, so after several runs the graph itself shows only the last run's files. Also
requires the gold table to carry its dbt test results (the dataQualityAssertions facet).

Env: MARQUEZ_URL (a port-forward to the API, scripts/e2e.sh); WAREHOUSE_DB (default warehouse).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MARQUEZ = os.environ["MARQUEZ_URL"].rstrip("/")
DB = os.environ.get("WAREHOUSE_DB", "warehouse")  # the dataset names' first part
# (gold table, a fixture the E2E landed into that feed, the feed's own intake job). The job
# is checked too: every file feed shares the bronze.csv_record node, so the landed file
# alone could be reached through another feed's models.
PATHS = [
    ("gold.member_eligibility", "healthcare.eligibility/elig_good.csv",
     "intake.healthcare.eligibility"),
    ("gold.invoice_match", "finance.vendor_invoice_pdf/invoice_scan.pdf",
     "intake.finance.vendor_invoice_pdf"),
]  # fmt: skip


def get(path: str) -> dict:
    with urllib.request.urlopen(MARQUEZ + path, timeout=15) as r:
        return json.loads(r.read())


def warehouse_namespaces() -> list[str]:
    """Every postgres:// namespace: the intake and dbt events name the warehouse by the DSN
    each was given, so there may be more than one; a table is looked up in each."""
    names = [n["name"] for n in get("/api/v1/namespaces?limit=100")["namespaces"]]
    return [n for n in names if n.startswith("postgres://")]


def ancestors(node_id: str) -> set[str]:
    try:
        graph = get("/api/v1/lineage?" + urllib.parse.urlencode({"nodeId": node_id, "depth": 20}))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # not a dataset in this namespace
            return set()
        raise
    nodes = {n["id"]: n for n in graph["graph"]}
    seen, todo = set(), [node_id]
    while todo:
        for edge in nodes.get(todo.pop(), {}).get("inEdges", []):
            if edge["origin"] not in seen:
                seen.add(edge["origin"])
                todo.append(edge["origin"])
    return seen


def main() -> int:
    deadline = time.monotonic() + 120
    spaces: list[str] = []
    while time.monotonic() < deadline and not (spaces := warehouse_namespaces()):
        time.sleep(5)
    if not spaces:
        print("FAIL: no warehouse (postgres://) namespace in Marquez", file=sys.stderr)
        return 1
    failures = []
    for table, landed, intake_job in PATHS:
        up: set[str] = set()
        for ns in spaces:
            up |= ancestors(f"dataset:{ns}:{DB}.{table}")
        if f"job:dip:{intake_job}" not in up:
            failures.append(f"{table}: {intake_job} is not upstream ({len(up)} ancestors)")
            continue
        # Marquez draws the graph from each job's latest version, whose inputs are the last
        # run's files; which file a run read is in the job's runs.
        runs = get(f"/api/v1/namespaces/dip/jobs/{urllib.parse.quote(intake_job, '')}"
                   "/runs?limit=100")  # fmt: skip
        ids = [
            v.get("datasetVersionId") or {}
            for r in runs.get("runs", [])
            for v in r.get("inputDatasetVersions", [])
        ]
        inputs = {f"{i.get('namespace', '')}|{i.get('name', '')}" for i in ids}
        if f"s3://landing|{landed}" not in inputs:
            failures.append(f"{table}: no run of {intake_job} read {landed}")
            continue
        jobs = sorted(a.split(":", 2)[2] for a in up if a.startswith("job:"))
        print(f"==> {landed} -> {intake_job} -> ... -> {table}: ok ({len(jobs)} jobs)")
    # The dbt job's output carries the model's test results (an output dataset facet).
    facets = {}
    for ns in spaces:
        try:
            facets |= get(f"/api/v1/namespaces/{urllib.parse.quote(ns, '')}/datasets/"
                          + urllib.parse.quote(f"{DB}.gold.member_eligibility", "")
                          ).get("facets", {})  # fmt: skip
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    if "dataQualityAssertions" not in facets:
        failures.append("gold.member_eligibility: no dbt test results (dataQualityAssertions)")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"PASS: lineage in Marquez for {len(PATHS)} landed file(s) to gold, with test results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
