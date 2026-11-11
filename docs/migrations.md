# Warehouse migrations (14.3)

The warehouse's own schema (the `ops` tables, `bronze`, the functions and the grants each
login gets) is a series of numbered SQL files in `dip/sql/migrations/`, applied in order and
once each by `dip/migrations.py`. dbt builds silver, gold and the research tiers on top; those
are not migrations.

## How it runs

- **At deploy.** The `warehouse-init` Job (`infra/terraform/platform_check.tf`) runs
  `migrate` with the warehouse owner's login before the platform check and the backups start.
  Its name carries a hash of the migration files and the image, so a new migration re-runs it.
- **In a task.** `Warehouse.init` (every intake task, the agent chain, the tests) reads
  `ops.schema_migration` and does nothing when every migration is applied. Before 14.3 it
  re-applied the whole DDL on every task, and its `ALTER TABLE` locks deadlocked with the MCP
  server's queries (#115's e2e, 2026-11-09).
- **By hand.** `python -m dip migrate` applies what is pending; `--status` lists them.

`migrate` takes an advisory lock and applies every pending migration in one transaction: all
of them or none. Each applied migration is a row in `ops.schema_migration` (version, file
name, SHA-256 of its text, time).

## Rules for a new migration

1. **A new file, never an edit.** `NNNN_short_name.sql`, the next number. An applied migration
   whose file has changed is refused (`MigrationChanged`), on deploy and in every task.
2. **Write it to run once.** It is applied once per warehouse, inside a transaction. Use
   `IF NOT EXISTS` where a table may already exist on a laptop's old database.
3. **Grants for a new login go in a migration too**, after the login is added to
   `infra/terraform/files/postgres-roles.sql` (the roles Job runs before `warehouse-init`).
   Grants are applied only when a migration runs, no longer by every task, so a new login or
   a new grant on an existing table is always a new migration (it can call the
   `ops.grant_*()` function again). A table dropped by hand is no longer recreated by the
   next task either: restore it (docs/backups.md).
4. **The upgrade workflow is the check.** It deploys the previous `main`, loads data, then
   deploys the PR: `warehouse-init` applies the new migrations to the previous release's
   database before the E2E runs again.

## Earlier releases and rollbacks

`0001_baseline.sql` is the old `dip/sql/warehouse.sql`, unchanged and idempotent, so a
warehouse built before 14.3 takes it without change and records it. A warehouse ahead of
the code (an older image after a rollback, 13.2) is not an error: versions the code does not
know are left alone.
