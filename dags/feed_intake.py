"""File-feed intake: one DAG per CSV, Excel or PDF data contract, generated from the contracts.

For each contract with `format: csv`, `excel` or `pdf` in DIP_CONTRACT_DIRS this defines a DAG
`intake__<pack>__<feed>`, for example `intake__healthcare__eligibility`. It takes every
file in `landing/<feed>/`, keeps an immutable raw copy, validates it against the
contract, and loads accepted rows into `bronze.csv_record` or quarantines the file or
row with reason codes in `ops.quarantine`. Then dbt builds and tests only the models
tagged with the feed name (bronze -> silver -> gold).

A PDF feed's intake task runs in the OCR image (images/ocr, `DIP_OCR_IMAGE`), the only
image with the PDF readers and Tesseract; without it set, its files are quarantined with
OCR.UNAVAILABLE rather than read.

The DAGs are triggered: by the sftp_sync DAG, the E2E test, or by hand. Late files
(`FEED.LATE`) are found by the feed_sla DAG from each contract's `expected_arrival`.
"""

from __future__ import annotations

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

from dip.contracts import dag_id_for, load_contracts


def _executor_config(format_type: str) -> dict:
    """PDF feeds read scans with Tesseract, which only the OCR image has (images/ocr): their
    intake task runs in it. Every other task keeps the Airflow image."""
    import os

    image = os.environ.get("DIP_OCR_IMAGE", "")
    if format_type != "pdf" or not image:
        return {}
    from kubernetes.client import models as k8s

    return {"pod_override": k8s.V1Pod(spec=k8s.V1PodSpec(
        containers=[k8s.V1Container(name="base", image=image)]
    ))}  # fmt: skip


def build(feed: str, format_type: str):
    @dag(
        dag_id=dag_id_for(feed),
        schedule=None,
        start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
        catchup=False,
        max_active_runs=1,
        tags=["intake", format_type, feed.split(".")[0]],
        doc_md=__doc__,
    )
    def feed_intake():
        @task(executor_config=_executor_config(format_type))
        def intake() -> dict:
            from dip import BUCKET_LANDING, lineage
            from dip.contracts import load_contracts
            from dip.dbt_runner import connection_dsn
            from dip.intake import process_file
            from dip.storage import S3Store
            from dip.warehouse import Warehouse

            store = S3Store(S3Hook(aws_conn_id="minio").get_conn())
            hook = PostgresHook(postgres_conn_id="warehouse")
            dsn = connection_dsn(hook.get_connection("warehouse"))
            emitter = lineage.Emitter()  # OPENLINEAGE_URL: Marquez, observability profile
            contract = load_contracts()[feed]
            statuses = []
            conn = hook.get_conn()
            try:
                warehouse = Warehouse(conn)
                warehouse.init()
                for key in store.list(BUCKET_LANDING, f"{feed}/"):
                    o = process_file(store, warehouse, contract, key)
                    lineage.intake_event(
                        emitter, dsn, feed, o, bronze=lineage.bronze_table(contract.format_type)
                    )
                    print(
                        f"{o.status:<24} {key} accepted={o.accepted} rejected={o.rejected}"
                        f" {list(o.reason_codes)}"
                    )
                    statuses.append(o.status)
            finally:
                conn.close()
            return {s: statuses.count(s) for s in sorted(set(statuses))}

        @task
        def transform(intake_summary: dict) -> None:
            from dip.dbt_runner import connection_dsn, run_dbt

            print(f"intake summary: {intake_summary}")
            dsn = connection_dsn(
                PostgresHook(postgres_conn_id="warehouse").get_connection("warehouse")
            )
            run_dbt(dsn, "build", "--select", f"tag:{feed}")

        transform(intake())

    return feed_intake()


for _contract in load_contracts().values():
    if _contract.format_type in ("csv", "excel", "pdf"):
        globals()[dag_id_for(_contract.feed)] = build(_contract.feed, _contract.format_type)
