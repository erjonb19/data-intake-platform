# Data contracts: CSV and Excel feeds

Every file feed has a YAML contract in its pack (`packs/<pack>/contracts/*.yaml`), checked
against `dip/contract.schema.json` when it loads. This page covers `format: csv` (Stage 1) and `format: excel` (Stage 2);
FHIR bundles are covered in [research/README.md](research/README.md).

Example: [`packs/healthcare/contracts/eligibility.yaml`](../packs/healthcare/contracts/eligibility.yaml).

## Keys

| Key | Meaning |
|---|---|
| `source.filename_pattern` | glob the landed file name must match, otherwise `FILE.NAME_PATTERN` |
| `source.filename_date` | the business date in the name: `regex` (one group), `format` (`strptime`, e.g. `%Y%m%d`), `max_days_before_received` (default 7), `max_days_after_received` (default 0). A name whose date is not a real date (the digits must round-trip exactly), is after the received UTC date, or is too far before it is `FILE.NAME_DATE`. A name with no match takes the date it was received |
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

## Excel (`format: excel`, Stage 2)

An `.xlsx` workbook is one sheet of rows with the same `columns`, rules and reason codes
as CSV: the reader (`dip/excelfeed.py`) turns cells into the text a CSV export would hold
and then runs the CSV checks, so a problem gets the same code in either format.

| Key | Meaning |
|---|---|
| `format.sheet` | the sheet to read, by name; without it, the first sheet. A missing sheet is `SCHEMA.MISSING_SHEET:<name>` |
| `format.header_row` | the row holding the header (default 1). Rows above it (a title, a logo row) are ignored |

Cells: an empty cell is `""`; whole numbers lose Excel's `.0` (`12`); dates are `YYYY-MM-DD`
(a time of midnight is a date); booleans are `true`/`false`. A formula reads as its cached
value; one never calculated by Excel has none and reads as empty, so `required` catches it.
Trailing empty columns and fully empty rows are dropped (empty rows counted as blank lines).
File codes added by Excel: `FILE.INVALID_XLSX` (not a readable workbook) and `FILE.MACROS`
(the workbook contains a VBA project; refused whatever its name).

## Channels (`source.channels`, Stage 2)

A file reaches `landing/<feed>/` through one of the channels its contract lists; a file
that arrives by any other channel is quarantined with `FILE.CHANNEL:<channel>` (before the
duplicate check, like a bad name, so the same bytes through an allowed channel still
load). The channel is recorded on each delivery (`ops.intake_delivery.channel`).

| Channel | How a file arrives |
|---|---|
| `minio` | put into the landing bucket directly (no channel metadata) |
| `sftp` | uploaded by the vendor to `/upload/<feed>/` on its own SFTP account (named after its pack, key-only, chrooted), then moved to landing by the `sftp_sync` DAG with `dip-channel: sftp` metadata. A file is taken once a `<name>.done` marker is next to it or it has been unchanged for 2 minutes; `.part`, `.tmp` and hidden files never are. An account can only send its own pack's feeds, and only feeds whose contract lists `sftp`; other folders are left in place and reported. If one account fails to sync, the others still go through and the run fails at the end. The server's host key is pinned on both sides (Terraform generates it). Known limit: the platform's SFTP key is in the environment of every Airflow pod, not only `sftp_sync`'s (a per-task pod override would narrow it) |
| `api` | `POST /v1/feeds/<feed>/files?filename=<name>` to the intake API with `Authorization: Bearer <key>`. One key per pack (Terraform generates them; the API stores only their SHA-256); a key may send only its pack's feeds. 401 no or unknown key, 403 another pack's feed or a feed without `api`, 404 unknown feed, 409 a same-named file still waiting in landing, 413 over 50 MB, 422 an unsafe file name. The file lands with `dip-channel: api`; the feed's intake DAG takes it from there |

Try it (after `scripts/up.sh`): `terraform -chdir=infra/terraform output -json sftp_vendor_keys`
gives each account's key, `output -raw sftp_host_key` the host key to pin, and
`kubectl -n dip-data port-forward svc/sftp 2222:22` the server. For the API,
`output -json intake_api_keys` and `port-forward svc/intake-api 8001:8000`.
`scripts/e2e.sh` does all of this.

## Reason codes

**File level** (the whole file goes to quarantine, nothing reaches bronze):

| Code | When |
|---|---|
| `FILE.NAME_PATTERN` | file name does not match `filename_pattern` |
| `FILE.NAME_DATE` | the date in the name fails `filename_date` (not a real date, in the future, or too old) |
| `FILE.ENCODING` | bytes are not valid in the contract's encoding |
| `FILE.PARSE_ERROR` | not parseable as delimited text (e.g. an unterminated quote) |
| `FILE.EMPTY` | no header row |
| `SCHEMA.MISSING_COLUMN:<col>` | a contract column is missing (schema drift) |
| `SCHEMA.UNEXPECTED_COLUMN:<col>` | a column not in the contract (schema drift) |
| `SCHEMA.DUPLICATE_COLUMN:<col>` | a column name appears twice |
| `FILE.TOO_FEW_ROWS` | fewer data rows than `min_rows` |
| `FILE.ROW_VIOLATIONS` | `reject_file` and at least one bad row |
| `FILE.BAD_ROW_RATIO` | `quarantine_row` and the bad-row share is above `max_bad_row_ratio` |

A file quarantined for its name is not recorded in `ops.intake_file`: its content was never
checked, so the same bytes under a correct name still load.

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
- Bookkeeping: `ops.intake_file` (one row per file content), `ops.intake_delivery` (one
  row per content and file name, with the validated business date) and `ops.quarantine`
  (one row per file or row violation, `record_ref = row=<n>`).
- Reruns: content with the same sha256 for the same feed is never loaded twice. Under the
  same name it is a `duplicate`. Under a new name it is `redelivered`: recorded as a new
  delivery, so a full-file feed that resends yesterday's exact content as today's file
  still has today's file as its latest.
- Silver and gold: dbt models tagged with the feed name, built by the feed's DAG
  (`dags/feed_intake.py` generates one per CSV contract: `intake__<pack>__<feed>`). For
  eligibility: `silver.eligibility_record` (every accepted row, typed, with the file's
  business date from its latest delivery) and `gold.member_eligibility`: one row per member ever
  listed, from the newest file that lists them, ordered by business date so a late older
  file never overrides newer data. The feed sends full files, so current members are the
  rows with `in_latest_file`; a disenrolled member, or one whose newest row was
  quarantined, keeps their last known row with `in_latest_file = false`.

## Healthcare eligibility (synthetic)

`packs/healthcare/generators/eligibility.py` writes a daily eligibility file from the
Synthea patients: one member per patient, with `member_id`, plan and coverage dates derived
deterministically from the patient id, and `patient_ref` linking back to the clinical record.
All of it is synthetic. Hand-written, invented test files are in `tests/fixtures/eligibility/`.

## Healthcare claims (synthetic, Stage 2)

`packs/healthcare/generators/claims.py` writes a daily claims file from the same Synthea
bundles: one row per **billed** line of each `ExplanationOfBenefit` (lines with an
adjudication), from the plan's start (2020-01-01) on, with the Blue Button line amounts
(billed, allowed, paid, coinsurance, deductible) and the member id eligibility gives that
patient. No new Synthea run is needed. Contract: `packs/healthcare/contracts/claims.yaml`
(`claims_YYYYMMDD.csv`, `claim_line_id` unique, amounts as non-negative decimals, at most
2% bad rows). On the 28 bundles in `data/synthea` it gives 1,976 lines, all of which pass (the file is
named for today by default: the contract accepts names up to 7 days old).

dbt: `silver.claim_record` (typed) and `gold.claim_line` (one row per line, from the
newest file that has it, so a corrected resend replaces it). Two DQ tests, warnings by
design, recorded in `ops.dq_results`:

| Test | Finds |
|---|---|
| `claims_within_coverage` | a line whose member is not in `gold.member_eligibility`, or whose service date is outside the member's coverage |
| `claims_amounts_ordered` | a line where paid > allowed, allowed > billed, or paid + coinsurance + deductible > allowed |

They warn rather than fail: a claim outside coverage is a finding for the payer to work,
not a reason to drop the line. Hand-written, invented test files are in
`tests/fixtures/claims/`.

## Finance invoices (generated, Stage 2)

`packs/finance/generators/invoices.py` writes the AP department's daily invoice register
as an Excel workbook (`invoices_YYYYMMDD.xlsx`, sheet `Invoices`, a title block above the
header on row 3), one row per invoice: number, vendor, dates, PO, payment terms, currency,
subtotal, tax (8.875%) and total. A seeded generator (no Faker), with the workbook's own
timestamps and the zip entries' dates fixed, so a seed and a date always give the same
bytes. `--breakage` adds one bad row each: `duplicate` (KEY.DUPLICATE), `negative_total`
(VALUE.OUT_OF_RANGE), `missing_po` (VALUE.REQUIRED), or `total_mismatch`, which passes the
contract and is found by dbt.

Contract: `packs/finance/contracts/vendor_invoice.yaml` (format `excel`, up to 10% bad rows).
dbt: `silver.vendor_invoice_record` and `gold.vendor_invoice` (one row per invoice number,
newest register wins; Stage 3's OCR invoices are matched to it). DQ warnings, in
`ops.dq_results`: `invoice_totals_add_up` (total = subtotal + tax, to the cent) and
`invoice_due_after_invoice`. Fixtures (`tests/fixtures/finance/`, from `make_fixtures.py`):
a clean register, one with every breakage, and one whose sheet was renamed
(SCHEMA.MISSING_SHEET).

## Late files (`expected_arrival`, Stage 2)

`expected_arrival: {cron: "0 6 * * *", grace: 2h}` says when the feed's file is due (a
5-field cron, UTC) and how late it may still arrive. Every 15 minutes the `feed_sla` DAG
(`dip/sla.py`) judges the latest window whose grace has passed: if nothing arrived since
the scheduled time before it (no delivery of any status, and no file quarantined for its
name: a bad file is not a late one), it opens one `FEED.LATE` row in `ops.incident` for that
feed and window. An incident closes when a file arrives; for a feed of daily full files one
arrival closes every open window. The cron and grace are checked when the contract loads.
Prometheus alerting on open incidents comes with Stage 3.
