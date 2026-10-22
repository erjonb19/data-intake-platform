# R7: the cohort service over the shareable tier

The clinical-drift-monitor's governed cohort service (`src/cdm/cohort.py` in
[erjonb19/clinical-drift-monitor](https://github.com/erjonb19/clinical-drift-monitor), merged
there as PR #4, "Phase G2") answers feasibility questions with suppressed counts. R7 lets it
read this platform's de-identified OMOP tier (`omop_shareable`) directly, read-only.

- **R7a, this repo:** done here. The sections below describe it.
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
| Grants, only for Synthea | macro `grant_cohort_reader` (dbt on-run-end) | The reader gets `USAGE` on `omop_shareable` and `SELECT` on its tables **only when `cdm_source` says `DIP-SYNTHEA`**. Otherwise every grant is revoked. `omop_shareable.concept` is never granted, because with a local Athena load it is the downloader's licensed copy |
| `omop_shareable.procedure_occurrence` | PR #20 | For the service's `procedure_concept_ids` criteria |

**The classification fails closed.**
- **`DIP-MIMIC`:** any MIMIC source is loaded, whatever else is loaded with it. That means
  an intake feed or relational table named `mimic.*`, or a build with
  `source_kind: mimic_relational`.
- **`DIP-SYNTHEA`:** no MIMIC source, the Synthea FHIR feed is loaded, and every other source
  is one of this repo's synthetic `healthcare.*` feeds.
- **`DIP-UNKNOWN`:** anything else, including an empty warehouse.

Tables that dbt rebuilds start with no grants, so a build that stops before on-run-end
leaves the reader with nothing.

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
  local `dip-dev-pg`), and even if it were, the grants would not be given.
- The reader can still *connect* to the cluster's other databases (`airflow`, `marquez`),
  because Postgres grants `CONNECT` to `PUBLIC`. It can read nothing there, since nothing is
  granted to `PUBLIC`.

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
