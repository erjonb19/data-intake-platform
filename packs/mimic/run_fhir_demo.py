"""M1: run the MIMIC-IV FHIR demo through the intake and into OMOP, locally.

    python packs/mimic/run_fhir_demo.py --dsn postgresql://.../mimic_fhir

REAL, de-identified data (PhysioNet, ODbL). Everything this writes stays local: the
warehouse named by --dsn, data/mimic/work/ and the report in data/mimic/reports/. Only
aggregate counts are printed.

Three passes, so the report can say exactly what breaks compared with Synthea:
  A. the export as delivered (bulk NDJSON files) against the Synthea contract   [dry run]
  B. regrouped into one bundle per patient, against the Synthea contract        [dry run]
  B2. as B with the Patient checks waived, to expose record-level breaks       [dry run]
  C. regrouped, against the MIMIC contract, loaded into --dsn, then dbt
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dip import BUCKET_LANDING, deid, vocab
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.fhir_bulk import bundle_export
from dip.intake import process_landing
from dip.storage import LocalStore
from dip.warehouse import DryRunWarehouse, Warehouse

EXPORT = REPO_ROOT / "data/mimic/mimic-iv-fhir-demo-2.1.0/fhir"
WORK = REPO_ROOT / "data/mimic/work/fhir"
REPORTS = REPO_ROOT / "data/mimic/reports"
SYNTHEA_FEED = "healthcare.fhir_patient_bundle"
MIMIC_FEED = "mimic.fhir_demo"
MIMIC_CONTRACTS = (REPO_ROOT / "packs/mimic/contracts",)
# In scope: what the OMOP tables built here use, and what the relational path (M2) uses
# too, so the reconciliation compares like with like. ICU charted events (millions of
# vital-sign rows), microbiology, administrations and dispenses are out of scope for now.
FILES = [
    "MimicPatient",
    "MimicEncounter",
    "MimicEncounterED",
    "MimicEncounterICU",
    "MimicCondition",
    "MimicConditionED",
    "MimicObservationLabevents",
    "MimicMedicationRequest",
    "MimicMedication",
    "MimicMedicationMix",
]
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _summary(wh: DryRunWarehouse | None, outcomes) -> dict:
    statuses = Counter(o.status for o in outcomes)
    out = {"files": len(outcomes), "statuses": dict(statuses)}
    if wh is not None:
        out["file_reasons"] = dict(
            Counter(q.reason_code for q in wh.quarantine if q.level == "file").most_common(15)
        )
        out["record_reasons"] = dict(
            Counter(q.reason_code for q in wh.quarantine if q.level == "record").most_common(15)
        )
        out["resources_accepted"] = wh.loaded
    return out


def pass_a() -> dict:
    """The export as delivered, into the Synthea feed."""
    store = LocalStore(WORK / "pass_a")
    for name in FILES:
        path = EXPORT / f"{name}.ndjson.gz"
        store.put(BUCKET_LANDING, f"{SYNTHEA_FEED}/{path.name}", path.read_bytes())
    wh = DryRunWarehouse()
    outcomes = process_landing(store, wh, load_contracts(), NOW)
    return _summary(wh, outcomes)


def _bundles(store_root: Path, feed: str) -> LocalStore:
    store = LocalStore(store_root)
    files = [EXPORT / f"{name}.ndjson.gz" for name in FILES]
    for pid, data in bundle_export(files, WORK / "spool"):
        store.put(BUCKET_LANDING, f"{feed}/{pid}.json", data)
    return store


def pass_b() -> dict:
    """Regrouped per patient, still against the Synthea contract."""
    store = _bundles(WORK / "pass_b", SYNTHEA_FEED)
    wh = DryRunWarehouse()
    contract = load_contracts()[SYNTHEA_FEED]
    # Report every record-level problem rather than stopping at the bad-row ratio.
    contract.spec["max_bad_row_ratio"] = 1.0
    outcomes = process_landing(store, wh, {SYNTHEA_FEED: contract}, NOW)
    return _summary(wh, outcomes)


def pass_b2() -> dict:
    """As B, but with the Patient checks waived. In B every file fails on the patient's
    (shifted) birth date, which hides what would break record by record."""
    store = _bundles(WORK / "pass_b2", SYNTHEA_FEED)
    wh = DryRunWarehouse()
    contract = load_contracts()[SYNTHEA_FEED]
    contract.spec["max_bad_row_ratio"] = 1.0
    contract.spec["fhir"]["resources"]["Patient"] = {}
    outcomes = process_landing(store, wh, {SYNTHEA_FEED: contract}, NOW)
    result = _summary(wh, outcomes)
    result["record_reasons"] = dict(
        Counter(q.reason_code for q in wh.quarantine if q.level == "record").most_common(25)
    )
    return result


def pass_c(dsn: str) -> dict:
    import psycopg

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for schema in ("bronze", "ops", "ops_secret", "silver", "omop", "deid", "omop_shareable"):
            cur.execute(f"drop schema if exists {schema} cascade")
    conn.commit()
    wh = Warehouse(conn)
    wh.init()
    store = _bundles(WORK / "pass_c", MIMIC_FEED)
    outcomes = process_landing(store, wh, load_contracts(MIMIC_CONTRACTS), NOW)
    result = _summary(None, outcomes)
    with conn.cursor() as cur:
        cur.execute(
            "select level, reason_code, count(*) from ops.quarantine group by 1, 2 order by 3 desc"
        )
        result["quarantine"] = [list(r) for r in cur.fetchall()]
        cur.execute(
            "select resource_type, count(*) from bronze.fhir_resource group by 1 order by 1"
        )
        result["bronze"] = dict(cur.fetchall())
    conn.commit()

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
    # Models first, then tests, so a failing DQ check still leaves the tables to inspect.
    result["dbt_seed_ok"] = run_dbt(dsn, "seed", check=False).returncode == 0
    result["dbt_run_ok"] = run_dbt(dsn, "run", check=False).returncode == 0
    result["dbt_test_ok"] = run_dbt(dsn, "test", check=False).returncode == 0
    with conn.cursor() as cur:
        counts = {}
        for table, concept in [
            ("person", "gender_concept_id"),
            ("visit_occurrence", "visit_concept_id"),
            ("condition_occurrence", "condition_concept_id"),
            ("measurement", "measurement_concept_id"),
            ("drug_exposure", "drug_concept_id"),
            ("observation", "observation_concept_id"),
        ]:
            cur.execute(f"select count(*), count(*) filter (where {concept} = 0) from omop.{table}")
            total, unmapped = cur.fetchone()
            counts[table] = {"rows": total, "unmapped": unmapped}
        result["omop"] = counts
        cur.execute(
            "select status, dq_category, test_name, failures from ops.dq_results"
            " where status <> 'pass' and invocation_id ="
            " (select invocation_id from ops.dq_results order by recorded_at desc limit 1)"
            " order by 2, 3"
        )
        result["dq_not_passing"] = [list(r) for r in cur.fetchall()]
    conn.commit()
    conn.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("MIMIC_FHIR_DSN"), required=False)
    parser.add_argument("--passes", default="ab2c")
    args = parser.parse_args()
    if not EXPORT.is_dir():
        raise SystemExit("run packs/mimic/fetch.sh fhir first")
    shutil.rmtree(WORK, ignore_errors=True)
    report: dict = {"source": "MIMIC-IV Clinical Database Demo on FHIR v2.1.0", "files": FILES}
    if "a" in args.passes:
        report["pass_a_as_delivered"] = pass_a()
    if "b" in args.passes:
        report["pass_b_bundled_synthea_contract"] = pass_b()
    if "2" in args.passes:
        report["pass_b2_bundled_synthea_contract_patient_checks_waived"] = pass_b2()
    if "c" in args.passes:
        if not args.dsn:
            raise SystemExit("--dsn (or MIMIC_FHIR_DSN) is required for pass c")
        report["pass_c_mimic_contract"] = pass_c(args.dsn)
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "fhir_demo_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nreport (local only, aggregates): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
