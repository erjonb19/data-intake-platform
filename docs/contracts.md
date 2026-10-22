# Data contracts: CSV feeds

Every file feed has a YAML contract in its pack (`packs/<pack>/contracts/*.yaml`), checked
against `dip/contract.schema.json` when it loads. This page covers `format: csv` (Stage 1);
FHIR bundles are covered in [research/README.md](research/README.md).

Example: [`packs/healthcare/contracts/eligibility.yaml`](../packs/healthcare/contracts/eligibility.yaml).

## Keys

| Key | Meaning |
|---|---|
| `source.filename_pattern` | glob the landed file name must match, otherwise `FILE.NAME_PATTERN` |
| `format` | `delimiter` (default `,`), `encoding` (default `utf-8`), `quotechar`, `header: true` |
| `columns[]` | `name`, `type` (`string`, `integer`, `decimal`, `date` as `YYYY-MM-DD`, `boolean`), `required`, `unique` (empty values are not keys), `pattern` (full match), `allowed` (values of the column's type), `min`/`max` for integer, decimal and date columns (`today` = the UTC date the file was received), `description`, `phi` (descriptive metadata for now: nothing enforces it yet; it is meant for the de-identification and masking steps later) |
| `allow_extra_columns` | default `false`: a column not in the contract is schema drift |
| `on_row_violation` | `quarantine_row`: bad rows go to quarantine, good rows load. `reject_file`: any bad row rejects the file |
| `max_bad_row_ratio` | with `quarantine_row`: more bad rows than this share rejects the whole file. Default 0, so without it any bad row rejects the file (reported as `FILE.BAD_ROW_RATIO`) |
| `min_rows` | default 1: a header-only file is rejected rather than read as "no members" |

Columns may come in any order; their names must match exactly after trimming surrounding
whitespace. Contract mistakes that the JSON Schema can't express (a regex that doesn't
compile, an unknown encoding, a `min`, `max` or `allowed` value of the wrong type) fail when
the contract loads, not when a file arrives.

## Reason codes

**File level** (the whole file goes to quarantine, nothing reaches bronze):

| Code | When |
|---|---|
| `FILE.NAME_PATTERN` | file name does not match `filename_pattern` |
| `FILE.ENCODING` | bytes are not valid in the contract's encoding |
| `FILE.PARSE_ERROR` | not parseable as delimited text (e.g. an unterminated quote) |
| `FILE.EMPTY` | no header row |
| `SCHEMA.MISSING_COLUMN:<col>` | a contract column is missing (schema drift) |
| `SCHEMA.UNEXPECTED_COLUMN:<col>` | a column not in the contract (schema drift) |
| `SCHEMA.DUPLICATE_COLUMN:<col>` | a column name appears twice |
| `FILE.TOO_FEW_ROWS` | fewer data rows than `min_rows` |
| `FILE.ROW_VIOLATIONS` | `reject_file` and at least one bad row |
| `FILE.BAD_ROW_RATIO` | `quarantine_row` and the bad-row share is above `max_bad_row_ratio` |

When a file is rejected for its rows (`FILE.ROW_VIOLATIONS`, `FILE.BAD_ROW_RATIO`), each
bad row's reason is still recorded: in `ops.quarantine` (level `record`) and in the file's
`.reason.json` under `rows`.

**Row level** (one row goes to quarantine as `<sha256>.rejected.ndjson`, with its row number
and values; the first failed check is the reason):

`ROW.FIELD_COUNT`, `VALUE.REQUIRED:<col>`, `VALUE.TYPE:<col>`, `VALUE.PATTERN:<col>`,
`VALUE.NOT_ALLOWED:<col>`, `VALUE.OUT_OF_RANGE:<col>`, `KEY.DUPLICATE:<col>`. For
`KEY.DUPLICATE` every row sharing a non-empty key is rejected, because the file doesn't say
which one is right, including when one of the copies also fails another check. A row of
empty fields (`,,,,`) is checked like any other row; only truly blank lines are skipped
(and counted).

## Where things land

- Raw copy: `raw/<feed>/<received date>/<sha256>.csv`, written first and never changed.
- Accepted rows: `bronze.csv_record` (feed, file sha256, row number, and the values as a
  jsonb object of strings, trimmed of surrounding whitespace and otherwise as received).
  Generic across feeds; each pack's silver models type their own feed.
- Bookkeeping: `ops.intake_file` (one row per file) and `ops.quarantine` (one row per file
  or row violation, `record_ref = row=<n>`).
- Reruns: a file with the same sha256 for the same feed is skipped (`duplicate`).
- Silver and gold: dbt models tagged with the feed name, built by the feed's DAG
  (`dags/feed_intake.py` generates one per CSV contract: `intake__<pack>__<feed>`). For
  eligibility: `silver.eligibility_record` (every accepted row, typed) and
  `gold.member_eligibility` (each member's current eligibility, from the latest file that
  lists them).

## Healthcare eligibility (synthetic)

`packs/healthcare/generators/eligibility.py` writes a daily eligibility file from the
Synthea patients: one member per patient, with `member_id`, plan and coverage dates derived
deterministically from the patient id, and `patient_ref` linking back to the clinical record.
All of it is synthetic. Hand-written, invented test files are in `tests/fixtures/eligibility/`.
