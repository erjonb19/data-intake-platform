# Runbook 1: a column renamed or dropped

**Signal.** The file is quarantined whole with `SCHEMA.MISSING_COLUMN:<col>` (and, for a
rename, `SCHEMA.UNEXPECTED_COLUMN:<new name>`). Alert **QuarantineRateHigh** if more than
20% of the feed's files in 24 hours were quarantined; the Feeds dashboard's "Files
quarantined whole" panel rises.

**Break it on purpose** (a good file in, a broken one out):

```bash
python -m dip break rename_column tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg column=plan_code --arg to=plan
python -m dip break drop_column   tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg column=coverage_start
```

## Evidence (MCP Read tier, or the same in SQL)

1. `list_quarantine_groups(feed)`: both codes, and the same file sha in each group, which
   says it is one problem seen twice.
2. `profile_file(feed, <sha prefix>)`: `missing_contract_columns` and the unexpected
   header; for a rename, the new column's counts and (if declared and not PHI) its values
   match the old column's.
3. `get_feed_history(feed)`: did earlier files have the column? A rename from one day on
   is deliberate; a column that comes and goes is a broken export.
4. `get_contract(feed)`: what rules did the column have? (Any declared column that is
   absent gives `SCHEMA.MISSING_COLUMN`, required or not.)

## Decide

| What you see | Root cause | Action |
|---|---|---|
| The same data under a new header, consistently from one file on | `schema_drift`, deliberate | `propose_contract_change`: the contract author renames the column and keeps its rules |
| A required column gone, nothing new in its place | `schema_drift`, a mistake | `request_resend` with the message naming the missing column |

The triage agent proposes one of these per group (`python -m dip triage run`); a person
approves it (`python -m dip approvals show <id>`, `approve <id>`, `execute <id>`). For a
contract change, the contract author's proposal becomes a local branch that the person
publishes (docs/agents/contract-author.md); `loosening` lists `<old column>: dropped`,
which for a rename is expected.

## Check

- After the vendor's corrected file, or after the contract change is merged and the file is
  replayed (`replay` re-lands it from its raw copy): `ops.intake_file.status = 'loaded'`.
- The group is gone from `list_quarantine_groups`; `query_metrics(quarantine_rate_24h,
  feed)` falls back as new files load.
