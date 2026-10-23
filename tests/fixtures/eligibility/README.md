Hand-written, invented eligibility files for the CSV contract tests and the Stage 1 E2E.
No real people: every name, id and date is made up.

- `good/elig_good.csv`: 6 valid members.
- `good/elig_sftp.csv`: 3 other valid members, delivered over SFTP in the E2E (Stage 2).
- `bad/elig_missing_member_id.csv`: the same members without the `member_id` column
  (schema drift): the whole file is quarantined with `SCHEMA.MISSING_COLUMN:member_id`.
- `bad/elig_bad_rows.csv`: 20 rows, 1 with an invalid plan code: that row is quarantined
  (`VALUE.NOT_ALLOWED:plan_code`), the other 19 load.
