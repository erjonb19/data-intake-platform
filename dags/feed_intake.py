"""File-feed intake: one DAG per CSV data contract, generated from the contracts (Stage 1).

For each contract with `format: csv` in DIP_CONTRACT_DIRS this defines a DAG
`intake__<pack>__<feed>`, for example `intake__healthcare__eligibility`. It takes every
file in `landing/<feed>/`, keeps an immutable raw copy, validates it against the
contract, and loads accepted rows into `bronze.csv_record` or quarantines the file or
row with reason codes in `ops.quarantine`. Then dbt builds and tests the models tagged
with the feed name (bronze -> silver -> gold).

The DAGs are triggered (by the E2E test, or by hand). Scheduling from the contract's
`expected_arrival` and late-file detection (`FEED.LATE`) arrive in Stage 2.
"""

from __future__ import annotations

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

from dip.contracts import load_contracts


def _dsn(hook: PostgresHook) -> str:
    from urllib.parse import quote

    c = hook.get_connection("warehouse")
    return f"postgresql://{quote(c.login)}:{quote(c.password)}@{c.host}:{c.port or 5432}/{c.schema}"


def dag_id_for(feed: str) -> str:
    return "intake__" + feed.replace(".", "__")


def build(feed: str):
    @dag(
        dag_id=dag_id_for(feed),
        schedule=None,
        start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
        catchup=False,
        max_active_runs=1,
        tags=["intake", "csv", feed.split(".")[0]],
        doc_md=__doc__,
    )
    def feed_intake():
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
            contract = load_contracts()[feed]
            statuses = []
            for key in store.list(BUCKET_LANDING, f"{feed}/"):
                o = process_file(store, warehouse, contract, key)
                print(
                    f"{o.status:<24} {key} accepted={o.accepted} rejected={o.rejected}"
                    f" {list(o.reason_codes)}"
                )
                statuses.append(o.status)
            return {s: statuses.count(s) for s in sorted(set(statuses))}

        @task
        def transform(intake_summary: dict) -> None:
            from dip.dbt_runner import run_dbt

            print(f"intake summary: {intake_summary}")
            dsn = _dsn(PostgresHook(postgres_conn_id="warehouse"))
            run_dbt(dsn, "build", "--select", f"tag:{feed}")

        transform(intake())

    return feed_intake()


for _contract in load_contracts().values():
    if _contract.format_type == "csv":
        globals()[dag_id_for(_contract.feed)] = build(_contract.feed)
