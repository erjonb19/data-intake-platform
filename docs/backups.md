# Warehouse backups

Stage 14, item 14.1. A nightly `pg_dump` of the `warehouse` database goes to the MinIO
bucket `backups`, the newest 7 dumps are kept, and every run leaves a row in `ops.backup`.
Item 14.2 adds the restore drill that proves a dump brings a table back: `dip drill
drop_table`, then `dip restore --latest --verify` (below). The E2E runs it on every push.

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
| The restore drill: `dip drill drop_table`, `dip restore --latest --verify` (14.2) | `dip/drills.py`, `dip/restore.py` |
| The drill's snapshot and result, one row per drill | `ops.restore_drill` in `dip/sql/warehouse.sql` |
| E2E: the drill on the dump just written, the time to restore in the job summary | `tests/e2e/restore.py` |

Dumps are named `warehouse/warehouse-<UTC start, YYYYMMDDTHHMMSSZ>.dump`, so their names sort
by time. After each good backup, every dump past the newest 7 (`backup_keep`) is deleted,
oldest first. Nothing else in the bucket is touched. A failed run deletes nothing.

`backups_enabled = false` in the Terraform variables removes the CronJob, its secrets and
the `dip-backups` alert group. The bucket stays, and so does the backup's MinIO user inside
MinIO: the Job that created it is removed, not the user. Delete it by hand if you switch
backups off for good: `mc admin user ls local` shows it as `warehouse-backup-<hash>`,
and `mc admin user rm local <that name>` removes it.

The backup's MinIO user can list, put and **delete** in `backups` (retention needs it). The
bucket has no versioning or object lock, so a leaked backup credential could delete every
dump. That is acceptable for a laptop cluster; a shared one would add object lock or a
second copy.

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

## The restore drill (14.2)

The drill drops a gold table on purpose, brings it back from the newest good dump, and
checks that it is the same table. Both commands refuse any kube context but `kind-dip`.

```bash
cd infra/terraform
export KUBECONFIG="$(terraform output -raw kubeconfig_path)"
export MINIO_USER="$(terraform output -raw minio_root_user)"
export MINIO_PASSWORD="$(terraform output -raw minio_root_password)"
cd ../..
python -m dip backup now                  # a dump that holds the table as it is now
python -m dip drill drop_table            # snapshot, then drop gold.member_eligibility
python -m dip restore --latest --verify   # bring it back and compare
```

**The drill** (`dip drill drop_table`, in `dip/drills.py`) refuses when a drop is already
waiting for its restore, when the table is not there, or when `ops.backup` has no good
backup. Otherwise, in one transaction in the live `warehouse`, it writes a row to
`ops.restore_drill` and drops the table. The row holds the row count, an order-independent
checksum, the owner, the grants and the newest good backup. The checksum is
`md5(string_agg(md5(t::text), '' order by md5(t::text)))`, taken with the time zone, date
style and float output fixed, so it compares the data and nothing else. There is no
CASCADE: a table that something depends on is not dropped, and nothing is recorded.

The snapshot is a table row, not a file, because the drill and the restore are two
commands, often run from different shells or CI steps. A row in the cluster is there for
whichever one runs the restore. The drill and the restore touch only `gold`, never `ops`.
The row also keeps the result, so every drill's time to restore stays on record.

**The restore** (`dip restore --latest --verify`, in `dip/restore.py`):

1. It reads the newest `ok` row of `ops.backup` and signs a GET URL for that dump, as the
   MinIO admin, valid for 5 minutes. The backup's own MinIO user cannot read a dump back.
2. `postgres-0` downloads the dump from MinIO inside the cluster with that URL (`wget`; the
   URL goes over stdin, never in a command's arguments). The restore then checks the
   file's size and SHA-256 in the pod against the row. A dump that does not match is never
   restored. The dump is not piped in over `kubectl exec -i`: in this drill's first CI run
   it arrived with a different SHA-256 (docs/incidents.md, 2026-11-09), and in 14.1
   pg_restore reading stdin that way never returned.
3. It drops and remakes the database `warehouse_restore`, then restores the whole dump into
   it with `pg_restore --exit-on-error`. It never restores over the live `warehouse`.
4. It brings the dropped table back into `warehouse` with `pg_restore -L`, in one
   transaction. The list holds the dump's entries for that table alone: its definition,
   its data, and the grants and comments on it. `pg_restore --table` alone would leave the
   grants behind. A table that is already there is not restored over.
5. It compares the count and checksum in `warehouse_restore` and in `warehouse`, and the
   live table's owner and grants, with the snapshot. It prints the time to restore, split
   by step, and records the result on the drill's row. It exits 1 if anything differs.

Without `--verify`, and with no drill waiting, it only restores into `warehouse_restore`,
to look at a backup next to the live data. `python -m dip drill drop_table --undo` is the
same as `restore --latest --verify`.

```sql
select drill_id, table_name, row_count, dropped_at, backup_id, restored_at,
       restore_backup_id, restore_seconds, verified, detail
from ops.restore_drill order by drill_id desc limit 5;
```

`warehouse_restore` stays until the next restore drops it. To remove it sooner:
`kubectl -n dip-data exec postgres-0 -- dropdb -U postgres warehouse_restore`.

The drill proves that the newest dump downloads intact, restores whole into an empty
database, and brings back one table with the same rows and grants. It does not prove a
recovery to the minute: whatever changed after the dump is lost, and the CronJob runs
nightly, so that can be up to a day. It does not rebuild a lost cluster either: the dump
holds no roles.

**In CI.** The kind E2E (`e2e.yml`, on every push) runs the drill on the dump that
`tests/e2e/backup.py` has just written (`tests/e2e/restore.py`). It takes its own count,
checksum and grants first, so the check does not rest on the code under test. The time to
restore goes in the job summary. The drill runs in the E2E and not as a proof-workflow job
because the proof workflow runs only when started by hand, and a job of its own would bring
up a second kind cluster for a few minutes of work (decisions log, PLAN.md, 2026-11-09).

## Restore the whole warehouse by hand

The dump holds everything in the `warehouse` database except the rows of `ops_secret` (24.12):
`ops_secret.deid_key`, the de-identification key, is dumped as an empty table, so the key
never sits in the bucket in plain text. After a restore, the next `fhir_intake` run installs
the key again from the `dip-deid` Secret (or run `python -m dip deid-key-install` with
`DIP_DEID_KEY` set); the key itself lives in that Secret and in Terraform's state, so keep a copy of it
(`terraform -chdir=infra/terraform output -raw` is not enough: read the `dip-deid` Secret) wherever the
owner keeps secrets, or a restored warehouse's pseudonyms cannot be reproduced. The rest of
the dump is still the warehouse's data: treat the bucket as secret. It holds no roles: those are cluster-wide
and come from `postgres-roles.sql` and the init script, which a fresh cluster already has.

`python -m dip restore --latest` (no drill waiting) does steps 1 to 3 below for you, into
`warehouse_restore`, with the hash checks. Switching the services over is still a person's
job.

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

3. Copy it into the pod as a file, and check its SHA-256 there against the row's `sha256`
   before going on: a copy piped over `kubectl exec -i` has arrived changed
   (docs/incidents.md, 2026-11-09), and pg_restore reading stdin that way hangs. Then
   restore it into a new database first, never straight over the live one:

   ```bash
   kubectl -n dip-data cp warehouse.dump postgres-0:/tmp/warehouse.dump
   kubectl -n dip-data exec postgres-0 -- sha256sum /tmp/warehouse.dump
   kubectl -n dip-data exec postgres-0 -- createdb -U postgres -O warehouse warehouse_restore
   kubectl -n dip-data exec postgres-0 -- \
     pg_restore -U postgres -d warehouse_restore --exit-on-error /tmp/warehouse.dump
   kubectl -n dip-data exec postgres-0 -- rm /tmp/warehouse.dump
   ```

4. Compare the row counts with the live database (for example `gold.member_eligibility`
   and `ops.intake_file`). Then either point the services at it, or rename the databases
   with the services scaled down.
