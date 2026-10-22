# R7: the cohort service over the shareable tier

The clinical-drift-monitor's governed cohort service (`src/cdm/cohort.py` in
[erjonb19/clinical-drift-monitor](https://github.com/erjonb19/clinical-drift-monitor), merged
there as PR #4, "Phase G2") answers feasibility questions with suppressed counts. R7 lets it
read this platform's de-identified OMOP tier (`omop_shareable`) directly, read-only.

- **R7a, this repo:** done here, including the longitudinal cohort library. The sections below describe it.
- **R7b, the drift monitor:** not built. [What the drift monitor needs](#what-the-drift-monitor-needs-r7b)
  is the list to hand over. It is written against that repo's `main` at `ed3822e`.

**Synthea only, never MIMIC.** The cohort service and its demo are public (the demo runs on
Streamlit Community Cloud). The MIMIC-IV demo is real patient data under ODbL and stays
local. Both repos enforce the rule, independently.

## What this repo provides (R7a)

| Piece | Where | What it does |
|---|---|---|
| Login role `dip_cohort_reader` | `infra/terraform/files/postgres-roles.sql` | Read-only by default (`default_transaction_read_only = on`), `statement_timeout` and `idle_in_transaction_session_timeout` of 60 s, at most 5 connections, `search_path = omop_shareable`. It is not a superuser, has no CREATEDB or CREATEROLE, and is a member of no role, `dip_researcher` included. The roles Job reapplies it on every `terraform apply` |
| Its password | Terraform `random_password` → Kubernetes secret → `postgres-roles-password.psql` | Set with psql's `\getenv`, with statement and error-statement logging turned off for that session, so the password isn't written to the Postgres log. It is never committed. Locally: `terraform -chdir=infra/terraform output -raw cohort_reader_dsn` (host port 15432, database `warehouse`) |
| `omop_shareable.cdm_source` | `dbt/models/shareable/share_cdm_source.sql` | The standard OMOP table, one row. `cdm_source_abbreviation` is `DIP-SYNTHEA`, `DIP-MIMIC` or `DIP-UNKNOWN`, with `vocabulary_version`, `cdm_version` (`v5.4`) and the build date |
| Grants, only for Synthea | macro `grant_cohort_reader` (dbt on-run-end) | The hook classifies the warehouse **live** with the same macro as `cdm_source` (`cdm_source_kind_sql`); it does not trust the `cdm_source` table, which a partial run may not have rebuilt. For anything but `DIP-SYNTHEA` it revokes every grant the reader has on the tier. MIMIC counts whatever its status, and the first run that sees it writes `ops.cohort_reader_block`: from then on the warehouse is `DIP-MIMIC` until a person deletes that row, after rebuilding from Synthea alone. So MIMIC rows that were later deleted or quarantined can't be forgotten while OMOP still holds what they fed. For `DIP-SYNTHEA` it grants USAGE on the schema and SELECT on exactly the shareable tables (never views) that this run rebuilt successfully, named one by one; a table it did not rebuild keeps what it had, which it was only ever given by a Synthea-only run that rebuilt it. `omop_shareable.concept` is never granted, because with a local Athena load it is the downloader's licensed copy |
| `omop_shareable.procedure_occurrence` | PR #20 | For the service's `procedure_concept_ids` criteria |

**The classification fails closed.**
- **`DIP-MIMIC`:** any MIMIC source is loaded, whatever else is loaded with it. That means
  an intake feed or relational table named `mimic.*`, or a build with
  `source_kind: mimic_relational`.
- **`DIP-SYNTHEA`:** no MIMIC source, the Synthea FHIR feed is loaded, and every other source
  is one of this repo's synthetic `healthcare.*` feeds.
- **`DIP-UNKNOWN`:** anything else, including an empty warehouse.

Tables that dbt rebuilds start with no grants. So a run that fails before on-run-end
leaves every table it rebuilt unreadable to the reader, and a run that rebuilt tables
while MIMIC was loaded leaves none of them granted.

**Feed names are the signal.** A MIMIC export sent through the Synthea feed would be
labelled by that feed's name. The guard there is the Synthea contract itself: MIMIC moves
every date past 2100, and the contract's future-date check rejects those resources, and
then the whole file on the bad-row ratio. (`packs/mimic/run_fhir_demo.py` waives those
checks on purpose to show what breaks, against a dry-run warehouse that writes nothing.)

**Tests.** `tests/integration/test_cohort_reader.py` applies the real role file, builds the
fixture patient, and connects as the reader. It checks that the reader:
- can read the tier through bare table names;
- can't read `omop`, `deid`, `ops`, `ops_secret`, `vocab`, `bronze` or the shareable `concept`;
- can't write: refused once by the read-only session, and again with `begin read write`.

Adding one MIMIC file, or one unclassified one, next to the Synthea data revokes everything.
Removing it restores the grants on the next build. Both MIMIC-shaped builds (FHIR and
relational) are checked to label themselves `DIP-MIMIC`.

**What it doesn't do.**
- The role exists in the cluster's Postgres. It is never created in a MIMIC warehouse (the
  local `dip-dev-pg`). If it were, the hook would still classify that warehouse as
  `DIP-MIMIC` live and grant nothing.
- The reader can still *connect* to the cluster's other databases (`airflow`, `marquez`),
  because Postgres grants `CONNECT` to `PUBLIC`. It can read nothing there, since nothing is
  granted to `PUBLIC`.

## The cohort library (R7, this repo)

The longitudinal part of R7 runs here: `packs/healthcare/cohorts/<name>/v<N>.sql` is a
library of versioned feasibility queries over `omop_shareable`, and `python -m dip cohort
run NAME` (`dip/cohorts.py`) is the only way one reaches the data. Each run:
- connects as `dip_cohort_reader` (and refuses any other role) and refuses unless
  `cdm_source` is exactly `DIP-SYNTHEA`: the same rule the warehouse's grant hook applies,
  checked again on this side;
- runs the file's single read-only query, which returns an attrition table;
- suppresses small cells: 1 to 10 is shown as `suppressed`, and so is any step whose drop
  from the last shown step is 1 to 10, so a hidden count cannot be recovered by
  subtraction; zero is shown;
- refuses an attrition table whose steps are not 0, 1, 2, ... or where a step is larger
  than the one before (a join that duplicated people would otherwise release two counts
  with a small difference);
- appends every outcome (ran, refused, failed) to a hash-chained JSONL audit log (`dip cohort
  verify-audit`), with the SHA-256 of the exact query file and what was released. The chain
  shows an edited, removed or reordered entry; it cannot show its last entries cut off or
  the whole file rewritten, so each run prints its entry's hash for the caller to keep.

Connecting means `current_user` **and** `session_user` are the reader, and the cdm_source
check and the query share one REPEATABLE READ snapshot. Differencing across cohorts, or
across rebuilds, is not checked by code: every library file is reviewed before it merges,
against what the library already releases.

Longitudinal criteria are SQL over the tier: diagnosed before a date, two results within a
year of the end of observation, a second diagnosis 30+ days after the first, a measurement
365+ days after another. Intervals within a person are exact on this tier; calendar dates
move by up to 180 days, which each cohort states as a caveat.

| Cohort | Question |
|---|---|
| `t2d_hba1c_monitoring` | adults with type 2 diabetes diagnosed before 2020, 2+ HbA1c in their last year, and one of 7.0%+ |
| `hypertension_bp_followup` | adults with hypertension observed a year after diagnosis, 2+ systolic readings in it, one of 140+ |
| `t2d_kidney_progression` | adults with type 2 diabetes with a kidney disease diagnosis 30+ days later, and within 5 years |
| `obesity_bmi_followup` | adults with a BMI of 30+, re-measured 365+ days later, and any such later BMI 5% lower |

**Measured on 568 Synthea patients** (500 living, seed 42, Synthea v4.0.0; full local
Athena vocabulary; released counts exactly as the library prints them):

| Cohort | Attrition (persons) |
|---|---|
| `t2d_hba1c_monitoring` | 568 → 44 adults with T2D → suppressed → 15 with 2+ HbA1c → 0 with a result of 7.0%+ |
| `hypertension_bp_followup` | 568 → 108 → suppressed → 36 with 2+ systolic readings → suppressed |
| `t2d_kidney_progression` | 568 → 44 → 16 with a later kidney disease diagnosis → suppressed |
| `obesity_bmi_followup` | 568 → 201 adults with a BMI of 30+ → 183 re-measured → 136 with a 5% lower BMI |

The zero in the first row reflects the source, not the query. Every HbA1c result is in %
(unit 8554, which the query requires), but Synthea's values are low: dbt's plausibility test
found 1,633 of 6,262 (26%) below 3%, the low end of a plausible range, across every year.
None of the 15 monitored patients has a result of 7.0% or more in their last year. The same build
also found events outside their visit's dates (81 drug, 74 measurement and 37 procedure
rows). Those tests are errors by design, so `dbt build` stops before the shareable tier;
the counts above come from `dbt run` with the findings reported here rather than hidden.
CI (`tests/integration/test_cohort_library.py`) runs every cohort as the reader on the
hand-made fixture patient. It checks each step's raw count for `t2d_hba1c_monitoring` on that
patient, and for the other three on six invented people built to stop at known steps
(inserted and rolled back in one transaction). It also covers refusals for the owner role,
for a tier the hook has revoked, and for a readable `cdm_source` that is not DIP-SYNTHEA.

## What the drift monitor needs (R7b)

Checked against `src/cdm/cohort.py` at `ed3822e`. Effort: about 1 day.

1. **A Postgres engine.** Add `postgres_engine(dsn) -> tuple[Engine, dict[str, str]]` next to
   `duckdb_engine`:
   - Use psycopg 3, as an optional dependency, for example a `postgres` extra.
   - Return `run(sql) -> pandas.DataFrame`.
   - Return the table map `{"person": "omop_shareable.person", "condition_occurrence":
     "omop_shareable.condition_occurrence", "procedure_occurrence":
     "omop_shareable.procedure_occurrence", "measurement": "omop_shareable.measurement"}`.
   - Read the DSN from an environment variable such as `CDM_OMOP_PG_DSN`, never from a file
     in the repo.
   - Open the connection read-only (`conn.read_only = True`) even though the role already
     is.
2. **Refuse anything but `DIP-SYNTHEA`, on every run.**
   - Before each run (not only at startup), `select cdm_source_abbreviation,
     vocabulary_version from omop_shareable.cdm_source`.
   - Raise `Refused`, audited like the other refusals, unless the result is exactly one row
     equal to `DIP-SYNTHEA`. A missing table, zero rows, `DIP-MIMIC` and `DIP-UNKNOWN` all
     refuse.
   - Put the abbreviation and vocabulary version into `CohortService.source`, so the
     hash-chained audit log records which tier answered.
   - This side's revoke makes the check redundant. It is there as defense in depth: each
     repo enforces the rule without trusting the other.
3. **Postgres has no `YEAR()`.** `compile_sql` uses `YEAR(c.condition_start_date)` for the
   index age. Write it as `EXTRACT(YEAR FROM c.condition_start_date)`, which DuckDB, Spark
   SQL and Postgres all accept, so one SQL text still runs on every engine. Postgres
   returns `numeric` for it. `FLOOR` and `CAST(... AS INTEGER)` in the `age_band` breakdown
   work unchanged, but cast `index_age` to an integer in the SQL so pandas gets integers.
   Run the existing DuckDB tests after the change. The rest of the generated SQL (CTEs,
   `UNION ALL ... ORDER BY step`, `GROUP BY 1, 2`, `IN (...)` lists) is plain SQL that
   Postgres runs as is.
4. **Tables that a tier doesn't have.** `OMOP_TABLES` includes `image_occurrence`, and this
   tier has none. Let a table map leave tables out. When a phenotype uses a criterion whose
   table isn't in the engine's map (`anatomic_site_concept_ids`, `modality_concept_ids`
   here), refuse it with a clear reason before any SQL runs. Today it would fail inside
   Postgres with "relation does not exist".
5. **A measurement criterion.** Add `"measurement_concept_ids": ("measurement",
   "measurement_concept_id")` to `CRITERION_KEYS`, and `measurement` to `OMOP_TABLES`
   (optional per point 4). Without it, "type 2 diabetes with an HbA1c result" can't be
   written. Adding drug criteria (`drug_concept_ids` on `drug_exposure`) the same way is
   optional.
6. **Concepts and a phenotype.**
   - Add the Synthea concepts to `config/omop_concepts.csv`: type 2 diabetes 201826, HbA1c
     (LOINC 4548-4) 3004410, colonoscopy 4249893. All three are in the Eunomia shard, which
     is what this repo's CI and the tests below use.
   - Add `phenotypes/t2d_with_hba1c/v1.yaml`: index condition 201826, ages 18 and up,
     include `measurement_concept_ids: [3004410]`, broken down by `gender_concept_id` and
     `age_band`.
   - It goes through the normal approval by hash.
7. **Tests stay offline.**
   - Load a small CC0 Synthea extract into DuckDB. Build it here with
     `python -m dip extract build --version <x>`; Synthea extracts are CC0. Its CSVs are the
     shareable tables, `procedure_occurrence` included.
   - In the DuckDB fixture, add a one-row `cdm_source` frame, plus a `DIP-MIMIC` variant to
     prove the refusal.
   - Test the Postgres engine against this repo's local stack only, behind a marker that is
     skipped when `CDM_OMOP_PG_DSN` is unset.
   - CI never reaches a database.
8. **The public demo never gets the DSN.** The DSN only works on the laptop (`localhost`
   NodePort), and the Streamlit app keeps using its committed data. Don't add the variable
   to Streamlit secrets.
9. **State the caveat in the service's docs and in answers.** This tier shifts every date
   per person by 1 to 180 days in either direction, keeps the year of birth only, and
   top-codes ages 90 and over. The service computes age as year of index minus year of
   birth, and both are shifted together, so an age can be one year off and a cut-off date
   can move by up to 180 days. Feasibility counts are approximate at the edges. The data is
   synthetic, but small-cell suppression stays on, so the demo behaves as it would on real
   data.

**Connection limits the service must live with:** read-only transactions, 60-second
statements, at most 5 connections. Bare table names work, because `search_path` is
`omop_shareable`.

## Try it locally (Synthea)

```bash
bash scripts/up.sh && bash scripts/e2e.sh   # loads the hand-made, Synthea-shaped fixture patient
export CDM_OMOP_PG_DSN="$(terraform -chdir=infra/terraform output -raw cohort_reader_dsn)"
psql "$CDM_OMOP_PG_DSN" -c "select cdm_source_abbreviation from cdm_source"   # DIP-SYNTHEA
psql "$CDM_OMOP_PG_DSN" -c "select count(*) from person"
```
