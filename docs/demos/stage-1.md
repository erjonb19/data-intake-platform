# Stage 1 demo: a thin vertical slice, end to end

**Goal:** a payer eligibility CSV dropped into MinIO becomes raw, then contract-checked,
then bronze, silver and gold, and a bad file lands in quarantine with its reason code.
The same run is the E2E test in CI. All data is synthetic.

## 1. Bring the stack up and run the E2E cases

```bash
bash scripts/up.sh
bash scripts/smoke.sh
bash scripts/e2e.sh        # the FHIR flow, then tests/e2e/cases.yaml
```

`e2e.sh` ends with `PASS: 3 end-to-end case(s) across 1 feed(s)`. The cases drop three
invented files into `landing/healthcare.eligibility/` and trigger the
`intake__healthcare__eligibility` DAG, which the DAG factory (`dags/feed_intake.py`)
generates from the contract:

| File | What happens |
|---|---|
| `elig_good.csv` | all 6 members reach `gold.member_eligibility`; the checksum over every column matches the file |
| `elig_missing_member_id.csv` | schema drift: the file is quarantined with `SCHEMA.MISSING_COLUMN:member_id`, and none of its rows reach bronze, silver or gold (each is checked) |
| `elig_bad_rows.csv` | 1 of 20 rows has an invalid plan code: that row is quarantined (`VALUE.NOT_ALLOWED:plan_code`), the other 19 load |

## 2. Look at it

- Airflow (http://localhost:8080): the `intake__healthcare__eligibility` DAG, with an
  `intake` and a `transform` task, each run in its own pod.
- MinIO (http://localhost:9001): `raw/healthcare.eligibility/<date>/<sha256>.csv` for every
  file, `quarantine/` with the bad file, its `.reason.json` and the rejected row as NDJSON.
- Postgres (`localhost:15432`, `bash scripts/creds.sh`):

```sql
select status, accepted_count, rejected_count, source_key from ops.intake_file
 where feed = 'healthcare.eligibility';
select level, reason_code, record_ref from ops.quarantine
 where feed = 'healthcare.eligibility';
select member_id, plan_code, file_date, in_latest_file from gold.member_eligibility
 order by member_id;
```

## 3. Try your own file

Generate a day's eligibility from the Synthea patients and drop it in:

```bash
python packs/healthcare/generators/eligibility.py data/synthea data/elig --date 20260102
# upload data/elig/elig_20260102.csv to landing/healthcare.eligibility/ (MinIO console),
# then trigger intake__healthcare__eligibility in the Airflow UI
```

Edit a copy first to see the checks: rename a column (`SCHEMA.MISSING_COLUMN`), add one
(`SCHEMA.UNEXPECTED_COLUMN`), or break a few `member_id` values (`VALUE.PATTERN`; more
than 5% of rows rejects the whole file with `FILE.BAD_ROW_RATIO`). Every key and reason
code is in [docs/contracts.md](../contracts.md).
