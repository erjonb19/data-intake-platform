# Runbook 4: delimiter or encoding changed

**Signal.** A delimiter change reads the whole header as one column: every declared
column is `SCHEMA.MISSING_COLUMN`, and one `SCHEMA.UNEXPECTED_COLUMN:<a;b;c...>` holds them
all. An encoding change is `FILE.ENCODING` ("not valid utf-8"). Both quarantine the file
whole.

**Break it on purpose:**

```bash
python -m dip break delimiter tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg "to=;"
```

## Evidence

1. `list_quarantine_groups`: the unexpected "column" is the old header joined by the new
   delimiter, which is the giveaway.
2. The contract author's `diff_schema` reports `file.sniffed_delimiter` and
   `file.encoding` ("not utf-8 (read as latin-1)").

## Decide

| What you see | Action |
|---|---|
| The vendor deliberately switched (the same columns, the same data) | `propose_contract_change`: `format.delimiter` or `format.encoding` |
| One odd file among good ones | `request_resend` |

## Check

The replayed file `loaded`, with the same row count as the vendor's previous files
(`get_feed_history`).
