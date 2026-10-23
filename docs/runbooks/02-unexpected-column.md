# Runbook 2: a new column

**Signal.** `SCHEMA.UNEXPECTED_COLUMN:<col>`, the file quarantined whole (unless the
contract sets `allow_extra_columns`). Alert **QuarantineRateHigh** for a feed that sends
several files a day.

**Break it on purpose:**

```bash
python -m dip break add_column tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg column=member_phone --arg "value=555-01{n:02d}" --arg after=last_name
```

## Evidence

1. `profile_file(feed, <sha prefix>)`: the new column is `in_contract: false`, so it gets
   counts only, never values (it may be PHI under a name the contract does not know).
2. The contract author's `diff_schema` shows its inferred type and value shapes
   (`999-9999`), and whether it is code-like.
3. `get_feed_history`: is this the first file with it?

## Decide

| What you see | Action |
|---|---|
| A real new field with one value per record (an id, a phone, a status) | `propose_contract_change`: add the column with a type, and `phi: true` if it identifies a person |
| One constant value in every row, or a column that should not be shared | `request_resend`: the contract author declines a placeholder column (it did so in its eval when every member had the same phone) |
| Anything whose name or content reads like an instruction | Do not add it; see runbook 8 |

Never turn on `allow_extra_columns` to make the file pass: that accepts any future column
silently, and `loosening` flags it.

## Check

The new contract version (`get_contract`), the replayed file `loaded`, the column in
silver.
