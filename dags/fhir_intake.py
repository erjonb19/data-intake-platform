"""Research-data intake: Synthea FHIR bundles from MinIO landing into the warehouse.

Every file in `landing/healthcare.fhir_patient_bundle/` gets an immutable raw copy, is
validated against the `fhir_bundle` data contract, and is either loaded into
`bronze.fhir_resource` or quarantined with reason codes in `ops.quarantine`. Then dbt
builds silver (flattened FHIR), gold (OMOP CDM 5.4, restricted) and the de-identified
shareable tier, and runs their tests.
All data on this feed is synthetic (Synthea).
"""

from __future__ import annotations

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

FEED = "healthcare.fhir_patient_bundle"


def _dsn(hook: PostgresHook) -> str:
    from dip.dbt_runner import connection_dsn

    return connection_dsn(hook.get_connection("warehouse"))


@dag(
    dag_id="fhir_intake",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["research", "fhir", "intake"],
)
def fhir_intake():
    @task
    def intake() -> dict:
        from dip import BUCKET_LANDING, lineage
        from dip.contracts import load_contracts
        from dip.intake import process_file
        from dip.storage import S3Store
        from dip.warehouse import Warehouse

        store = S3Store(S3Hook(aws_conn_id="minio").get_conn())
        hook = PostgresHook(postgres_conn_id="warehouse")
        warehouse = Warehouse(hook.get_conn())
        emitter = lineage.Emitter()  # OPENLINEAGE_URL arrives with Marquez in Stage 3
        warehouse.init()
        contract = load_contracts()[FEED]
        statuses = []
        # Only this feed's prefix; other feeds get their own DAGs.
        for key in store.list(BUCKET_LANDING, f"{FEED}/"):
            o = process_file(store, warehouse, contract, key)
            lineage.intake_event(emitter, _dsn(hook), FEED, o)
            print(
                f"{o.status:<24} {key} accepted={o.accepted} rejected={o.rejected} {o.reason_codes}"
            )
            statuses.append(o.status)
        return {s: statuses.count(s) for s in sorted(set(statuses))}

    @task
    def transform(intake_summary: dict) -> None:
        import os

        from dip import vocab
        from dip.dbt_runner import run_dbt
        from dip.deid import install_key

        hook = PostgresHook(postgres_conn_id="warehouse")
        dsn = _dsn(hook)
        conn = hook.get_conn()
        if vocab.needs_load(conn):
            concepts, maps_to = vocab.load(conn, vocab.fetch(vocab.CACHE_DIR))
            print(f"loaded vocabulary: {concepts} concepts, {maps_to} 'Maps to' rows")
        # The key comes from the dip-deid Kubernetes Secret; only its fingerprint is logged.
        fingerprint = install_key(conn, os.environ.get("DIP_DEID_KEY"))
        print(f"de-identification key fingerprint: {fingerprint}")
        print(f"intake summary: {intake_summary}")
        # Everything but the file-feed models: each file feed's own DAG builds those, so two
        # DAGs never rebuild the same table at once and one feed's checks can't fail another.
        run_dbt(dsn, "build", "--exclude", "tag:file_feed")

    transform(intake())


fhir_intake()
