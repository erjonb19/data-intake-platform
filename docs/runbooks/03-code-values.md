# Runbook 3: a new or junk code value

**Signal.** `VALUE.NOT_ALLOWED:<col>` on rows (`level = 'record'`), the rest of the file
loaded (`loaded_with_rejections`), or the whole file quarantined with `FILE.BAD_ROW_RATIO`
when more than `max_bad_row_ratio` of the rows fail. Alert **RowRejectionRateHigh** above
10% of rows in 24 hours.

With the commands below (a new code on 1 row in 3, junk on 1 in 4), more than the
eligibility contract's `max_bad_row_ratio` (0.05) of the rows fail, so the file is
quarantined whole with `FILE.BAD_ROW_RATIO`, each bad row still carrying
`VALUE.NOT_ALLOWED:plan_code`. Use a larger `every` (say 30) to see the row-level case.

**A changed value format** works the same way with `VALUE.TYPE:<col>`: a vendor that starts
writing dates as MM/DD/YYYY fails every row's date check
(`python -m dip break type_change <good> <out> --arg column=dob`), so the file is
quarantined whole with `FILE.BAD_ROW_RATIO`. The contract's date type is ISO 8601; ask the
vendor to resend in it (`request_resend`) rather than loosening the contract.

**Break it on purpose:**

```bash
python -m dip break new_code    tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg column=plan_code --arg value=GOLD --arg every=3
python -m dip break junk_values tests/fixtures/onboarding/elig_base.csv out/elig_20261021.csv --arg column=plan_code --arg value=??? --arg every=4
```

## Evidence

1. `get_quarantine_sample(group)`: the rejected values (shown for a declared, non-PHI
   column; a PHI column's values are hidden).
2. `get_contract(feed)`: the `allowed` list.
3. `query_metrics(open_quarantine, feed)`: how many rows, and whether it keeps growing.

## Decide

| What you see | Action |
|---|---|
| A well-formed new code used consistently (`GOLD` on a third of the rows) | `propose_contract_change`: add it to `allowed`; `loosening` lists exactly `<col>: allowed` |
| Junk (`???`), a typo, a value that varies at random | `request_resend`, or `discard` the rows if the data is not wanted |

The contract author's evals include both cases; it proposes the first and declines the
second (docs/agents/EVALS.md).

## Check

`release` of the quarantined rows is not an action: rows quarantined against an old
contract are fixed at the source. After the contract change is merged, the next file loads
the new code; the old rows stay as the record of what was refused.
