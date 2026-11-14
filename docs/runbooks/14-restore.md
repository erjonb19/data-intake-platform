# Runbook 14: a warehouse table lost, restored from a backup

**Signal.** A gold table is gone or wrong: a query or dashboard fails with
`relation "gold.<table>" does not exist`, a dbt model that reads it fails (a `DAG.FAILED`
incident whose task log says so), or rows were deleted by mistake. Backups themselves have
their own alert, **BackupMissing** (no good backup in 26 hours, from
`dip_backup_last_success_timestamp_seconds`).

The `warehouse-backup` CronJob in `dip-data` writes a `pg_dump` of the `warehouse` database
to `s3://backups/warehouse/` every night at 02:15 UTC and keeps the newest 7; each run is a
row in `ops.backup`. A restore brings back the table as it was at the last good dump:
anything written after that dump is lost and has to be loaded again.

**Break it on purpose (kind only):** `python -m dip drill drop_table` records
`gold.member_eligibility`'s row count, checksum, owner and grants in `ops.restore_drill` and
drops it; `python -m dip restore --latest --verify` brings it back and compares. The kind E2E
runs it on every push.

## Evidence

1. Is there a good dump to restore from: `ops.backup`, newest first
   (`status`, `finished_at`, `object_key`, `bytes`, `sha256`, `error`). No `ok` row, or none
   recent: there is nothing to restore; rebuild the table from its sources instead.
2. What the table should hold: `ops.restore_drill` for a drill (the snapshot), otherwise the
   row counts and the latest loads in `ops.intake_file` for the feeds that build it.
3. For a failed run: `get_dag_run` and `get_task_log` on the DAG whose task failed, for the
   missing relation's name.

## Decide

A warehouse table that was dropped or damaged: what to do, by what the backups show.

| What you see | Action |
|---|---|
| The table is gone and `ops.backup` has a good dump from after it was last right | Restore it: `python -m dip restore --latest --verify` after a drill, or `python -m dip restore --latest` to restore into `warehouse_restore` and bring the table back by hand (`docs/backups.md`); never restore over the live `warehouse` |
| The table is there but its rows are wrong | Restore into `warehouse_restore` (`python -m dip restore --latest`), compare it with the live table, and copy back what was lost |
| No good backup, or the newest is older than the damage | Rebuild from the sources: re-run the feeds' intake and dbt for the period; check why backups stopped (**BackupMissing**, the CronJob's last Job) |
| Files landed after the dump | After the restore, re-run their intake DAGs, or they are missing from the table |
| The drill (`gold.member_eligibility`, a waiting row in `ops.restore_drill`) | Expected: `python -m dip restore --latest --verify` (or `dip drill drop_table --undo`) |

## Check

The restore prints the time to restore and exits 0 only when the count, checksum, owner and
grants match the snapshot; the drill's row in `ops.restore_drill` has `verified = true`. The
failed DAG run, cleared, now passes, and its `DAG.FAILED` incident closes on its next
success.
