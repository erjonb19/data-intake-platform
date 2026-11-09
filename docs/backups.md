# Warehouse backups

Stage 14, item 14.1. A nightly `pg_dump` of the `warehouse` database goes to the MinIO
bucket `backups`, the newest 7 dumps are kept, and every run leaves a row in `ops.backup`.
The restore drill that proves a dump brings the warehouse back is 14.2 (not built yet). Until
then, the manual steps below are the restore.

## What runs

| Piece | Where |
|---|---|
| CronJob `warehouse-backup` in `dip-data`, nightly at 02:15 UTC (`backup_schedule`) | `infra/terraform/backups.tf` |
| Step 1, `dump`: `pg_dump --format=custom`, then `pg_restore --list` to check the archive | init container, `var.postgres_image` |
| Step 2, `upload`: the `ops.backup` row, the upload, the size check, retention | `dip/backup.py` (Airflow image) |
| The login `dip_backup`: `pg_read_all_data`, plus INSERT and UPDATE on `ops.backup` | `infra/terraform/files/postgres-roles.sql`, `ops.grant_backup()` in `dip/sql/warehouse.sql` |
| The MinIO user: list, put and delete in `backups` only (it cannot read a dump back) | `infra/terraform/backups.tf` |
| `dip_backup_last_success_timestamp_seconds` and the `BackupMissing` alert (no good backup in 26 h) | `dip/metrics.py`, `observability/prometheus/rules.yml` |
| `python -m dip backup now`: one run now, as a Job made from the CronJob (kind-dip only) | `dip/backup.py` |
| E2E: one run, then the object, its hash and `pg_restore --list` | `tests/e2e/backup.py` |

Dumps are named `warehouse/warehouse-<UTC start, YYYYMMDDTHHMMSSZ>.dump`, so their names sort
by time. After each good backup, every dump past the newest 7 (`backup_keep`) is deleted,
oldest first. Nothing else in the bucket is touched. A failed run deletes nothing.

`backups_enabled = false` in the Terraform variables removes the CronJob, its MinIO user and
secrets, and the `dip-backups` alert group. The bucket stays.

Memory: the dump step requests 64 Mi (limit 256 Mi) and the upload step 128 Mi (limit
384 Mi). They run one after the other, so the pod requests 128 Mi, for a few minutes a night.

## Run one now and check it

```bash
KUBECONFIG="$(terraform -chdir=infra/terraform output -raw kubeconfig_path)" \
  python -m dip backup now
```

It prints the Job's name, `ok` or `failed`, and the upload step's summary (the key, bytes,
SHA-256 and the dumps it pruned). Each run's outcome is in the warehouse:

```sql
select backup_id, started_at, finished_at, status, object_key, bytes, sha256, error
from ops.backup order by backup_id desc limit 10;
```

A failed dump has `status = 'failed'` and pg_dump's stderr in `error`. A dump that is good
while retention failed has `status = 'ok'` and an `error` that starts with `retention:`.
The Job then fails too.

## Restore by hand (until 14.2)

The dump holds everything in the `warehouse` database, including `ops_secret.deid_key`, the
de-identification key. Treat the bucket as secret. It holds no roles: those are cluster-wide
and come from `postgres-roles.sql` and the init script, which a fresh cluster already has.

1. Find the newest good dump:

   ```bash
   kubectl -n dip-data exec postgres-0 -- psql -U postgres -d warehouse -Atc \
     "select object_key from ops.backup where status = 'ok' order by finished_at desc limit 1"
   ```

2. Download it as the MinIO admin. The NodePort is `127.0.0.1:9000`, and the credentials
   are `terraform output -raw minio_root_user` and `minio_root_password`:

   ```python
   import os

   import boto3

   s3 = boto3.client(
       "s3",
       endpoint_url="http://127.0.0.1:9000",
       aws_access_key_id=os.environ["MINIO_USER"],
       aws_secret_access_key=os.environ["MINIO_PASSWORD"],
       region_name="us-east-1",
   )
   s3.download_file("backups", os.environ["KEY"], "warehouse.dump")
   ```

3. Restore it into a new database first, never straight over the live one:

   ```bash
   kubectl -n dip-data exec postgres-0 -- createdb -U postgres -O warehouse warehouse_restore
   kubectl -n dip-data exec -i postgres-0 -- \
     pg_restore -U postgres -d warehouse_restore --exit-on-error < warehouse.dump
   ```

4. Compare the row counts with the live database (for example `gold.member_eligibility`
   and `ops.intake_file`). Then either point the services at it, or rename the databases
   with the services scaled down.

14.2 turns this into a drill in the proof workflow: drop a table, restore, and verify
counts and checksums against the snapshot taken before the drop.
