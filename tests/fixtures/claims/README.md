Hand-written, invented claims files for the claims contract tests and the Stage 2 E2E.
No real people or claims: every id, code use, date and amount is made up. Member ids match
the invented members in `../eligibility/good/elig_good.csv`.

- `good/claims_good.csv`: 5 valid lines. Two are DQ findings, on purpose: M000000005's
  line is after their coverage ended (2025-12-31), and M000000999 is not a member. Both
  load; `claims_within_coverage` reports them as warnings.
- `bad/claims_missing_paid_amount.csv`: the `paid_amount` column is missing (schema
  drift): the whole file is quarantined with `SCHEMA.MISSING_COLUMN:paid_amount`.
