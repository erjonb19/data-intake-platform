"""SFTP channel (Stage 2): move complete vendor uploads from the SFTP server to landing,
then trigger the intake DAG of each feed that received files.

Logs in to every account in DIP_SFTP_ACCOUNTS with the platform's key (DIP_SFTP_KEY) and
checks the server against its pinned host key (DIP_SFTP_HOST_KEY): an unknown host key is
refused, never accepted on first use. All four come from the dip-sftp Secret that
Terraform writes. The rules for what is taken are in dip/sftp_sync.py.

Scheduled every 10 minutes; like every DAG here it starts paused, and the E2E test
triggers it directly.
"""

from __future__ import annotations

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task


@dag(
    dag_id="sftp_sync",
    schedule="*/10 * * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["intake", "sftp"],
    doc_md=__doc__,
)
def sftp_sync():
    @task
    def sync() -> dict:
        import base64
        import io
        import os
        from datetime import UTC, datetime

        import paramiko

        from dip.contracts import dag_id_for, load_contracts
        from dip.sftp_sync import sync_account
        from dip.storage import S3Store

        host = os.environ["DIP_SFTP_HOST"]
        key_type, key_b64 = os.environ["DIP_SFTP_HOST_KEY"].split()[:2]
        pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(os.environ["DIP_SFTP_KEY"]))
        store = S3Store(S3Hook(aws_conn_id="minio").get_conn())
        contracts = load_contracts()
        feeds: set[str] = set()
        failed: list[str] = []
        for account in os.environ["DIP_SFTP_ACCOUNTS"].split(","):
            ssh = paramiko.SSHClient()
            pinned = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64))
            ssh.get_host_keys().add(host, key_type, pinned)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            # One account failing (auth, network) must not strand the others' files: carry
            # on, trigger what moved, and fail the run at the end (check_accounts).
            try:
                ssh.connect(
                    host, username=account, pkey=pkey, look_for_keys=False, allow_agent=False,
                    timeout=30,
                )  # fmt: skip
                r = sync_account(ssh.open_sftp(), account, store, contracts, datetime.now(UTC))
            except Exception as exc:
                failed.append(f"{account}: {type(exc).__name__}: {exc}")
                print(f"FAILED  {account}: {type(exc).__name__}: {exc}")
                continue
            finally:
                ssh.close()
            for _, key in r.moved:
                print(f"moved   {account}: {key}")
            for path in r.waiting:
                print(f"waiting {account}: {path}")
            for path, why in r.refused:
                print(f"refused {account}: {path}: {why}")
            feeds.update(r.feeds)
        # Only feeds with a generated intake DAG (CSV and Excel); FHIR has its own DAG.
        dag_ids = [dag_id_for(f) for f in sorted(feeds)
                   if contracts[f].format_type in ("csv", "excel")]  # fmt: skip
        return {"dag_ids": dag_ids, "failed": failed}

    @task
    def dag_ids(result: dict) -> list[str]:
        return result["dag_ids"]

    @task(trigger_rule="all_done")
    def check_accounts(result: dict) -> None:
        if result["failed"]:
            raise RuntimeError(f"accounts that failed to sync: {result['failed']}")

    result = sync()
    triggers = TriggerDagRunOperator.partial(task_id="trigger_intake").expand(
        trigger_dag_id=dag_ids(result)
    )
    triggers >> check_accounts(result)


sftp_sync()
