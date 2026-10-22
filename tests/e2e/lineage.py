"""Stage 3 (S3.3) end-to-end check of lineage in Marquez, after the file-feed cases.

For each path below, walks Marquez's lineage graph upstream from the gold table (the
graph's inEdges) and requires the landed file among its ancestors: the intake job read the
file and wrote bronze, and the dbt jobs carried it to gold. Also requires the gold table to
carry its dbt test results (the dataQualityAssertions facet).

Env: MARQUEZ_URL (a port-forward to the API, scripts/e2e.sh); WAREHOUSE_DB (default warehouse).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

MARQUEZ = os.environ["MARQUEZ_URL"].rstrip("/")
DB = os.environ.get("WAREHOUSE_DB", "warehouse")  # the dataset names' first part
# (gold table, a fixture the E2E landed into that feed)
PATHS = [
    ("gold.member_eligibility", "healthcare.eligibility/elig_good.csv"),
    ("gold.invoice_match", "finance.vendor_invoice_pdf/invoice_scan.pdf"),
]


def get(path: str) -> dict:
    with urllib.request.urlopen(MARQUEZ + path, timeout=15) as r:
        return json.loads(r.read())


def warehouse_namespace() -> str | None:
    names = [n["name"] for n in get("/api/v1/namespaces?limit=100")["namespaces"]]
    return next((n for n in names if n.startswith("postgres://")), None)


def ancestors(node_id: str) -> set[str]:
    graph = get("/api/v1/lineage?" + urllib.parse.urlencode({"nodeId": node_id, "depth": 20}))
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
    ns = None
    while time.monotonic() < deadline and not (ns := warehouse_namespace()):
        time.sleep(5)
    if not ns:
        print("FAIL: no warehouse (postgres://) namespace in Marquez", file=sys.stderr)
        return 1
    failures = []
    for table, landed in PATHS:
        node = f"dataset:{ns}:{DB}.{table}"
        up = ancestors(node)
        want = f"dataset:s3://landing:{landed}"
        if want not in up:
            failures.append(f"{table}: {landed} is not upstream ({len(up)} ancestors)")
            continue
        jobs = sorted(a.split(":", 2)[2] for a in up if a.startswith("job:"))
        print(f"==> {landed} -> ... -> {table}: ok ({len(jobs)} jobs: {', '.join(jobs[:4])}...)")
    # The dbt job's output carries the model's test results (an output dataset facet).
    ds = get(f"/api/v1/namespaces/{urllib.parse.quote(ns, '')}/datasets/"
             + urllib.parse.quote(f"{DB}.gold.member_eligibility", ""))  # fmt: skip
    if "dataQualityAssertions" not in ds.get("facets", {}):
        failures.append("gold.member_eligibility: no dbt test results (dataQualityAssertions)")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print(f"PASS: lineage in Marquez for {len(PATHS)} landed file(s) to gold, with test results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
