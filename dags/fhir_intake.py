"""Research-data intake: Synthea FHIR bundles from MinIO landing into the warehouse.

Every file in `landing/healthcare.fhir_patient_bundle/` gets an immutable raw copy, is
validated against the `fhir_bundle` data contract, and is either loaded into
`bronze.fhir_resource` or quarantined with reason codes in `ops.quarantine`.
All data on this feed is synthetic (Synthea).
"""

from __future__ import annotations

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

FEED = "healthcare.fhir_patient_bundle"


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
        from dip import BUCKET_LANDING
        from dip.contracts import load_contracts
        from dip.intake import process_file
        from dip.storage import S3Store
        from dip.warehouse import Warehouse

        store = S3Store(S3Hook(aws_conn_id="minio").get_conn())
        warehouse = Warehouse(PostgresHook(postgres_conn_id="warehouse").get_conn())
        warehouse.init()
        contract = load_contracts()[FEED]
        statuses = []
        # Only this feed's prefix; other feeds get their own DAGs.
        for key in store.list(BUCKET_LANDING, f"{FEED}/"):
            o = process_file(store, warehouse, contract, key)
            print(
                f"{o.status:<24} {key} accepted={o.accepted} rejected={o.rejected} {o.reason_codes}"
            )
            statuses.append(o.status)
        return {s: statuses.count(s) for s in sorted(set(statuses))}

    intake()


fhir_intake()
