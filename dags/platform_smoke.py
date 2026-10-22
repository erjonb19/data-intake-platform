"""Stage 0 smoke DAG: proves the platform's moving parts work together.

Each task runs in its own KubernetesExecutor pod and checks one dependency:
MinIO buckets exist, and the warehouse database and its schemas are reachable.
"""

from __future__ import annotations

import socket

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task

REQUIRED_BUCKETS = ("landing", "raw", "quarantine", "airflow-logs")
REQUIRED_SCHEMAS = ("bronze", "silver", "gold", "ops")


@dag(
    dag_id="platform_smoke",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    tags=["platform", "smoke"],
)
def platform_smoke():
    @task
    def check_minio() -> dict:
        client = S3Hook(aws_conn_id="minio").get_conn()
        found = {b["Name"] for b in client.list_buckets()["Buckets"]}
        missing = sorted(set(REQUIRED_BUCKETS) - found)
        if missing:
            raise RuntimeError(f"missing MinIO buckets: {missing}")
        return {"pod": socket.gethostname(), "buckets": sorted(found)}

    @task
    def check_warehouse() -> dict:
        rows = PostgresHook(postgres_conn_id="warehouse").get_records(
            "select schema_name from information_schema.schemata where schema_name = any(%s)",
            parameters=(list(REQUIRED_SCHEMAS),),
        )
        found = {r[0] for r in rows}
        missing = sorted(set(REQUIRED_SCHEMAS) - found)
        if missing:
            raise RuntimeError(f"missing warehouse schemas: {missing}")
        return {"pod": socket.gethostname(), "schemas": sorted(found)}

    @task
    def report(minio: dict, warehouse: dict) -> None:
        # Different hostnames show each task ran in its own pod.
        print(f"minio checked in pod {minio['pod']}: {minio['buckets']}")
        print(f"warehouse checked in pod {warehouse['pod']}: {warehouse['schemas']}")

    report(check_minio(), check_warehouse())


platform_smoke()
