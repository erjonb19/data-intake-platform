"""M2: the MIMIC-IV relational demo through the relational-tables-to-OMOP path, locally.

    python packs/mimic/run_relational_demo.py --dsn postgresql://.../mimic_rel

REAL, de-identified data (PhysioNet, ODbL). Everything stays local: the warehouse named by
--dsn and the report in data/mimic/reports/. Only aggregate counts are printed.

Steps: load the tables in packs/mimic/relational/relational_demo.yaml into schema `mimic`
(header, type, key and not-null checks), load the vocabulary and the de-identification
key, then dbt with source_kind=mimic_relational: the same staging contract and OMOP models
as the FHIR path.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets

from dip import deid, relational, vocab
from dip.contracts import REPO_ROOT
from dip.dbt_runner import run_dbt

SOURCE = REPO_ROOT / "data/mimic/mimic-iv-demo-2.2"
CONTRACT = REPO_ROOT / "packs/mimic/relational/relational_demo.yaml"
REPORTS = REPO_ROOT / "data/mimic/reports"
VARS = ["--vars", "{source_kind: mimic_relational}"]
OMOP_TABLES = [
    ("person", "gender_concept_id"),
    ("visit_occurrence", "visit_concept_id"),
    ("condition_occurrence", "condition_concept_id"),
    ("measurement", "measurement_concept_id"),
    ("drug_exposure", "drug_concept_id"),
    ("observation", "observation_concept_id"),
]


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("MIMIC_REL_DSN"))
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("--dsn (or MIMIC_REL_DSN) is required")
    if not SOURCE.is_dir():
        raise SystemExit("run packs/mimic/fetch.sh relational first")

    conn = psycopg.connect(args.dsn)
    with conn.cursor() as cur:
        for schema in ("mimic", "ops", "ops_secret", "silver", "omop", "deid", "omop_shareable"):
            cur.execute(f"drop schema if exists {schema} cascade")
    conn.commit()
    report: dict = {"source": "MIMIC-IV Clinical Database Demo v2.2 (relational)"}
    results = relational.load_all(conn, relational.load_contract(CONTRACT), SOURCE)
    report["tables"] = {
        r.table: {"status": r.status, "rows": r.rows, "reasons": r.reason_codes} for r in results
    }
    if any(r.status != "loaded" for r in results):
        print(json.dumps(report, indent=2))
        raise SystemExit("a source table was quarantined; see the reasons above")

    # The warehouse DDL (ops, ops_secret, pgcrypto, roles) is shared with the FHIR path.
    from dip.warehouse import Warehouse

    Warehouse(conn).init()
    # Athena when DIP_ATHENA_DIRS is set, else Eunomia; plus mimic-code's lab -> LOINC map.
    vocab.load_configured(conn, REPO_ROOT / "data/cache")
    vocab.load_source_to_concept_map(
        conn,
        vocab.MIMIC_LAB_VOCABULARY,
        vocab.read_mimic_lab_map(vocab.fetch_mimic_lab_map(REPO_ROOT / "data/cache")),
    )
    key_file = REPO_ROOT / "data/mimic/deid.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    deid.install_key(conn, key_file.read_text().strip())
    report["dbt_seed_ok"] = run_dbt(args.dsn, "seed", *VARS, check=False).returncode == 0
    report["dbt_run_ok"] = run_dbt(args.dsn, "run", *VARS, check=False).returncode == 0
    report["dbt_test_ok"] = run_dbt(args.dsn, "test", *VARS, check=False).returncode == 0
    with conn.cursor() as cur:
        omop = {}
        for table, concept in OMOP_TABLES:
            cur.execute(f"select count(*), count(*) filter (where {concept} = 0) from omop.{table}")
            total, unmapped = cur.fetchone()
            omop[table] = {"rows": total, "unmapped": unmapped}
        report["omop"] = omop
        cur.execute(
            "select status, dq_category, test_name, failures from ops.dq_results"
            " where status <> 'pass' and invocation_id ="
            " (select invocation_id from ops.dq_results order by recorded_at desc limit 1)"
            " order by 2, 3"
        )
        report["dq_not_passing"] = [list(r) for r in cur.fetchall()]
    conn.commit()
    conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "relational_demo_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nreport (local only, aggregates): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
