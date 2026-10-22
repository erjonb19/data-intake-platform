# Stage 2 demo: every channel and format, both packs

**Goal:** files arrive three ways (straight into MinIO, over SFTP, and through the intake
API), as CSV, Excel and FHIR; the healthcare pack carries eligibility, claims and the
research track, the finance pack carries vendor invoices; bad rows and bad files land in
quarantine with reason codes, and a feed whose file does not come opens a `FEED.LATE`
incident. The same run is the E2E test in CI. All data is synthetic or generated.

## 1. Bring the stack up and run the E2E cases

```bash
bash scripts/up.sh          # builds the Airflow and intake API images, deploys everything
bash scripts/smoke.sh
bash scripts/e2e.sh         # FHIR flow, then tests/e2e/cases.yaml, then feed_sla
```

`e2e.sh` port-forwards the SFTP server and the intake API (no new host ports, so an
existing cluster needs no rebuild), writes each vendor's SFTP key to a private temporary
directory, and removes it at the end.

| Feed | Fixture | Channel | What happens |
|---|---|---|---|
| healthcare.eligibility | `elig_good.csv` | MinIO | 6 members to gold, checksum over every column |
| | `elig_missing_member_id.csv` | MinIO | `SCHEMA.MISSING_COLUMN:member_id`, nothing reaches bronze |
| | `elig_bad_rows.csv` | MinIO | 1 of 20 rows quarantined (`VALUE.NOT_ALLOWED:plan_code`), 19 load |
| | `elig_sftp.csv` | **SFTP** | uploaded with the vendor's key and a `.done` marker; `sftp_sync` moves it and triggers the feed; the delivery is recorded as `sftp` |
| | `elig_api.csv` | **API** | a 401 without a key and a 403 with the other pack's key first, then accepted; recorded as `api` |
| healthcare.claims | `claims_good.csv` | MinIO | 5 lines to `gold.claim_line`; 2 coverage findings as DQ warnings |
| | `claims_missing_paid_amount.csv` | MinIO | `SCHEMA.MISSING_COLUMN:paid_amount` |
| finance.vendor_invoice | `invoices_good.xlsx` | MinIO | **Excel**: 12 invoices to `gold.vendor_invoice` |
| | `invoices_breakages.xlsx` | MinIO | 4 rows quarantined (duplicates, a negative total, a missing PO), 37 load; a wrong total is a DQ warning |
| | `invoices_sheet_renamed.xlsx` | MinIO | `SCHEMA.MISSING_SHEET:Invoices` |

Then the `feed_sla` DAG runs and records into `ops.incident`.

## 2. Look at it

```sql
-- how each delivery arrived
select feed, channel, count(*) from ops.intake_delivery group by 1, 2 order by 1, 2;
-- what was quarantined, and why
select feed, level, reason_code, count(*) from ops.quarantine group by 1, 2, 3 order by 1, 3;
-- DQ findings that warn rather than fail
select test_name, status, failures from ops.dq_results where status = 'warn' order by 1;
-- late files
select feed, kind, window_start, closed_at from ops.incident order by window_start;
```

## 3. Try the channels by hand

```bash
cd infra/terraform
terraform output -json sftp_vendor_keys     # each vendor account's key
terraform output -raw sftp_host_key         # pin this in known_hosts
terraform output -json intake_api_keys      # each client's API key
kubectl -n dip-data port-forward svc/sftp 2222:22 &
kubectl -n dip-data port-forward svc/intake-api 8001:8000 &
curl -X POST "http://127.0.0.1:8001/v1/feeds/healthcare.eligibility/files?filename=elig_20261022.csv" \
  -H "Authorization: Bearer <healthcare key>" --data-binary @tests/fixtures/eligibility/good/elig_good.csv
```

Details: [docs/contracts.md](../contracts.md) (formats, channels, late files, the claims and
finance packs). The upgrade job (`.github/workflows/upgrade.yml`) deploys the previous
version, loads data, upgrades the same cluster to this one and reruns all of the above.
