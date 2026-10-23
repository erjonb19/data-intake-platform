"""Late files (Stage 2): every 15 minutes, open a FEED.LATE incident for each feed whose
expected file (the contract's expected_arrival) is past its grace window with nothing
received, and close incidents whose file has since arrived. Rules in dip/sla.py.
Like every DAG here it starts paused."""

from __future__ import annotations

import pendulum
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task


@dag(
    dag_id="feed_sla",
    schedule="*/15 * * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["sla", "monitoring"],
    doc_md=__doc__,
)
def feed_sla():
    @task
    def check() -> dict:
        from datetime import UTC, datetime

        from dip.contracts import load_contracts
        from dip.sla import check as check_feeds

        conn = PostgresHook(postgres_conn_id="warehouse").get_conn()
        try:
            changes = check_feeds(conn, load_contracts(), datetime.now(UTC))
        finally:
            conn.close()
        for kind in ("opened", "closed"):
            for item in changes[kind]:
                print(f"{kind:<7} FEED.LATE {item}")
        return changes

    check()


feed_sla()
