# Research-data track

PLAN.md §8.1 describes the plan. This page documents what is built, item by item.

> **Data in R1–R6 is synthetic.** Patients come from [Synthea](https://github.com/synthetichealth/synthea)
> (v4.0.0, fixed seed) or from small hand-written fixtures in `tests/fixtures/fhir/`. The
> feed is still classified `restricted` and handled as if it were PHI, so the controls are
> realistic. The MIMIC-IV demos (PLAN.md §8.2) are **real, de-identified** data, used
> locally only and never committed: see [mimic.md](mimic.md).

## R1. FHIR intake

```
Synthea (pinned jar, seed 42, reference and end date 2026-01-01)
   │  one FHIR R4 transaction Bundle per patient
   ▼
landing/healthcare.fhir_patient_bundle/<file>.json          (MinIO)
   │  fhir_intake DAG ─ dip.intake.process_file
   ├─▶ raw/<feed>/<date>/<sha256>.json                       immutable copy, written first
   ├─▶ contract check: packs/healthcare/contracts/fhir_patient_bundle.yaml
   │     file level   ─ fail ─▶ quarantine/<feed>/<date>/<sha256>.json + .reason.json
   │     record level ─ fail ─▶ quarantine/<feed>/<date>/<sha256>.rejected.ndjson
   ▼
bronze.fhir_resource (jsonb, one row per accepted resource)
ops.intake_file   (one row per file: status, counts, contract version)
ops.quarantine    (one row per quarantined file or record, with a reason code)
```

### Try it

```bash
pip install -e ".[dev]"
bash packs/healthcare/generators/synthea.sh 25 data/synthea   # needs Docker, not Java
pytest -q                                                      # unit tests
DIP_TEST_DSN=postgresql://... DIP_SYNTHEA_DIR=data/synthea pytest -q -s tests/integration
```

On the kind stack: `bash scripts/up.sh && bash scripts/smoke.sh && bash scripts/e2e.sh`.

### The contract

The `fhir_bundle` contract type (`dip/contract.schema.json`) adds a `fhir` block:

| Setting | Meaning |
|---|---|
| `patient_resource` | Exactly one per file; if it fails a check, the whole file is rejected |
| `load` | Resource types loaded to bronze. Others (Claim, ExplanationOfBenefit, CarePlan, ...) are counted and skipped: data minimization |
| `resources.<Type>.required` | Dotted paths that must be present and non-empty |
| `resources.<Type>.allowed` | Value sets for a path |
| `resources.<Type>.required_one_of` | At least one of these paths must be present (a MedicationRequest codes its drug inline or through a Medication resource) |
| `resources.<Type>.code_systems` | At least one coding must use one of these systems (SNOMED, LOINC, RxNorm) |
| `resources.<Type>.dates` | Must parse as FHIR dates and must not be after the file arrived |
| `references` | `subject`, `encounter` and `medicationReference` must point at an *accepted* resource in the same bundle, so rejecting an Encounter also rejects what was recorded during it |
| `max_bad_row_ratio` | Above this share of rejected resources, the file is rejected (`CONTRACT.BAD_ROW_RATIO`) |

### Reason codes

| Level | Code | When |
|---|---|---|
| file | `FILE.INVALID_JSON` | Not UTF-8 JSON |
| file | `FILE.NAME_PATTERN` | Filename does not match the contract |
| file | `FHIR.NOT_A_BUNDLE`, `FHIR.BUNDLE_TYPE:<type>`, `FHIR.EMPTY_BUNDLE`, `FHIR.MALFORMED_ENTRY` | Structural |
| file | `FHIR.PATIENT_COUNT:<n>` | Not exactly one Patient |
| file | `FHIR.PATIENT_INVALID:<record code>` | The Patient itself failed a check |
| file | `CONTRACT.BAD_ROW_RATIO`, `CONTRACT.ROW_VIOLATION` | Too many bad records, or `on_row_violation: reject_file` |
| record | `FHIR.MISSING_FIELD:<Type>.<path>` | Required field absent |
| record | `FHIR.INVALID_VALUE:<Type>.<path>` | Not in the allowed values |
| record | `FHIR.UNKNOWN_CODE_SYSTEM:<Type>.code` | No coding in the expected system |
| record | `FHIR.INVALID_DATE:<Type>.<path>`, `FHIR.FUTURE_DATE:<Type>.<path>` | Bad or future date |
| record | `FHIR.UNRESOLVED_REFERENCE:<Type>.<field>` | Points at nothing, or at a rejected resource |
| record | `FHIR.DUPLICATE_ID:<Type>` | Same id twice in one bundle |

### Guarantees and how they are tested

| Guarantee | Test |
|---|---|
| A rejected file contributes no rows to bronze | `tests/unit/test_intake.py`, `tests/e2e/fhir_intake.py` |
| Every file keeps a raw copy addressed by sha256 | `test_good_and_bad_files` |
| Reprocessing the same file is a no-op | `test_rerun_of_same_file_is_a_duplicate` |
| Bronze, quarantine and intake rows commit together | `test_failed_write_leaves_nothing_behind` (Postgres) |
| The contract fits real Synthea output (no file-level rejections) | `test_real_synthea_export`, run in CI on 25 fresh Synthea patients |

## R2. OMOP CDM gold layer (dbt)

```
bronze.fhir_resource ──▶ silver (views, dbt/models/silver)          ──▶ omop (tables, dbt/models/omop)
                          fhir_resource_current  latest version         person
                          fhir_patient                                   observation_period
                          fhir_encounter                                 visit_occurrence
                          fhir_condition                                 condition_occurrence
                          fhir_observation       incl. components        measurement
                          fhir_medication_request incl. Medication refs  drug_exposure
                          fhir_procedure         done, not not-done       procedure_occurrence
                          source_to_standard     code → concept          concept
vocab.concept, vocab.concept_relationship ◀── python -m dip vocab-load (Eunomia shard, pinned + sha256;
                                               or local Athena folders, never committed)
```

```bash
export DIP_WAREHOUSE_DSN=postgresql://user:pass@localhost:5432/db
python -m dip db-init && python -m dip land healthcare.fhir_patient_bundle data/synthea/*.json
python -m dip intake && python -m dip vocab-load && python -m dip dbt build
```

In the cluster, the `fhir_intake` DAG runs `intake` then `transform` (vocabulary load if
missing, then `dbt build`) in KubernetesExecutor pods.

### Mapping decisions

| OMOP field | From | Rule |
|---|---|---|
| `*_id` keys | FHIR resource id | `omop_id()`: a stable 60-bit hash, so a person keeps the same `person_id` as new files arrive. Uniqueness is a dbt test |
| `gender_concept_id` | `Patient.gender` | male 8507, female 8532, otherwise 0 |
| `race_concept_id`, `ethnicity_concept_id` | US Core race/ethnicity OMB category | `seeds/fhir_concept_map.csv`; absent or other → 0 |
| `visit_concept_id` | `Encounter.class` | AMB 9202 outpatient, EMER 9203 ER, IMP/ACUTE 9201 inpatient, other → 0 |
| `condition_concept_id` | SNOMED code (ICD-10 when Synthea gives only that) | source concept → "Maps to" → standard concept |
| `measurement_*` | `Observation` value and each component | routed here when the LOINC code's standard domain is Measurement, or it is an unmapped laboratory/vital-sign result. Units via UCUM |
| `drug_concept_id` | RxNorm on the order, or on its `Medication` (MIMIC: NDC, else a local name) | "Maps to" standard, Drug-domain concepts only: an NDC that maps to a Device stays here with concept 0 and its source concept (no `device_exposure` table); end date = start date (orders carry none) |
| `procedure_concept_id` | SNOMED code on `Procedure` (CDT for Synthea's dental procedures; contract v2) | "Maps to" standard, Procedure-domain concepts only. A procedure whose standard concept is in another domain (a throat culture is a Measurement) stays here with concept 0 and its source concept: OMOP would move it, which this build does not do yet. With the Eunomia shard, 1,515 of 3,429 Synthea procedures (44%) map; 913 (27%) are in another domain, 717 codes are not in the shard, and 280 are CDT, which the shard lacks. Status `not-done` and `entered-in-error` are left out. MIMIC procedures are not loaded yet |
| `observation_period` | all of the above | one period per person, first to last event |
| `*_type_concept_id` | | 32817 (EHR) everywhere |

Unmapped codes keep `concept_id = 0` and their `*_source_value`, as OMOP requires. The
vocabulary in CI is a **shard**, not full Athena, so the unmapped rate on real Synthea
output is higher than in production; CI prints it per table, and R3 turns it into
thresholds. Locally a full Athena download can be loaded instead: Synthea's unmapped rows
drop from 6.8% to 0.1% ([vocabulary.md](vocabulary.md)).

Concepts the shard lacks (gender, race, the EHR type concept, concept 0) are in
`dbt/seeds/omop_fixed_concepts.csv`, labeled with where they come from.

**Not built yet:**
`death`, `provider`, `care_site`, and drug/condition eras.

### Tests

`tests/integration/test_omop_dbt.py` runs `dbt build` (models plus PK, FK and not-null
tests) on the fixture patient plus 25 Synthea patients, then checks known values: the
fixture's type 2 diabetes is concept 201826, its three HbA1c results are concept 3004410
in % (8554), its visits are outpatient (9202), its metformin order is concept 40163924,
its observation period runs 2018-03-02 to 2025-08-18, and `person_id` is unchanged by a
rebuild.

## R3. OMOP data quality

The checks are dbt generic tests (`dbt/tests/generic/dqd.sql`), each named after the
[OHDSI Data Quality Dashboard](https://github.com/OHDSI/DataQualityDashboard) check it
mirrors. **We do not run DQD itself**: it is an R package and would need R installed on the
laptop and in CI. The equivalents run inside every `dbt build`, and an `on-run-end` hook
records each result in `ops.dq_results` (run id, test, table, column, category, DQD check
name, status, failing rows).

| Category | DQD check | What it asserts here | Severity |
|---|---|---|---|
| Conformance | `isPrimaryKey`, `isRequired` | dbt `unique`/`not_null` on keys and required dates | error |
| Conformance | `isForeignKey` | person, visit FKs (`relationships`); type and period concepts exist in CONCEPT | error |
| Conformance | `fkDomain` | gender/race/ethnicity/visit/condition/measurement/drug/unit/value concepts are in the right domain | error |
| Conformance | `isStandardValidConcept` | non-zero clinical concepts are standard and not invalidated | error |
| Completeness | `standardConceptRecordCompleteness` | share of `concept_id = 0` per table, under a threshold | warn |
| Completeness | `sourceValueCompleteness` | unmapped rows still carry their source code | error |
| Completeness | `measurePersonCompleteness` | share of persons with no rows in a table, under a threshold | warn |
| Plausibility | `plausibleAfterBirth` | event dates are not before birth | error |
| Plausibility | `plausibleStartBeforeEnd` | start ≤ end for visits, conditions, drugs, observation periods | error |
| Plausibility | `withinVisitDates` | events fall within their visit (±7 days, as DQD) | error |
| Plausibility | `plausibleValueLow/High` | HbA1c 3–20 %, BMI 8–100, height 20–250 cm, weight 0.5–400 kg, systolic 40–300, diastolic 20–200 mmHg, glucose 10–1000 mg/dL, heart rate 20–250 /min, year of birth 1850–now | error |
| Plausibility | `plausibleUnitConceptIds` | HbA1c is recorded in % (8554) | error |
| Plausibility | *(OMOP convention, not a DQD check)* `inObservationPeriod` | every event is inside the person's observation period | error |

**Thresholds** for the warnings are set against the measured rates on Synthea v4 with the
Eunomia shard: unmapped conditions ≤ 50%, drugs ≤ 30% (measured 20%), measurements ≤ 10%
(4%), visits ≤ 5% (1%). Conditions measure **about 50%** once Observation-domain records
are routed out: 50.1% on the first run, and 49.7% after R5 pinned Synthea's end date. So
that check sits right at its threshold. It is the honest signal that the shard is too
small for Synthea v4's codes. With a full Athena vocabulary it measures 2.4%.

Latest CI run (R5, 25 Synthea patients + fixture): 154 dbt nodes, PASS=153, WARN=1
(4 conditions outside their visit window), ERROR=0.

**Not covered** (compared with DQD's ~3,500 parameterized checks): `cdmField`/`cdmDatatype`
(DDL conformance; our models define the columns), `plausibleGender`, `plausibleBeforeDeath`
(no DEATH table yet), and checks on tables not built yet.

**What the checks found on the first run** (25 Synthea patients), and what changed:

1. `fkDomain` on `condition_occurrence` failed for 196 rows. Synthea records SNOMED
   *situations* and *social findings* (for example "Medication review due", employment
   status) as FHIR Conditions, and their standard concepts are in OMOP's Observation
   domain. Fix: route by domain, as OMOP requires. These now go to a new OMOP
   `observation` table, along with Observation-domain FHIR Observations (smoking status,
   surveys) that R2 had been dropping.
2. `withinVisitDates` on conditions failed for 7 of about 800 rows: Synthea records some
   onsets weeks before the encounter that recorded them. That is how the source behaves,
   not a pipeline bug, so it is a warning (DQD also treats this check with a threshold).

**Proof the checks bite:** `test_dq_catches_implausible_hba1c` inserts an HbA1c of 55%,
runs the check, and asserts that it fails and that the failure is recorded in
`ops.dq_results`.

```sql
-- Latest DQ run, by category
select dq_category, status, count(*) from ops.dq_results
where invocation_id = (select invocation_id from ops.dq_results order by recorded_at desc limit 1)
group by 1, 2 order by 1, 2;
```

## R4. De-identified shareable tier and governed access

| Tier | Schemas | Who can read |
|---|---|---|
| Restricted | `bronze`, `silver`, `omop`, `deid`, `ops_secret` | the pipeline (`warehouse` role) only |
| Shareable | `omop_shareable` | `dip_researcher` (read-only), plus the pipeline |

`omop_shareable` has the same OMOP tables, derived from `omop` (dbt `models/shareable/`):

| Transformation | How |
|---|---|
| Pseudonymous ids | every id is `HMAC-SHA256(key, "<table>:<id>")` truncated to 60 bits. Without the key a pseudonym can't be linked back, and pseudonyms for different tables are unrelated |
| Date shifting | one offset per person, 1–180 days in either direction (never 0), derived from the key, applied to **every** date and timestamp of that person, so intervals ("2 HbA1c results within a year") are exact |
| Birth | year only, taken from the shifted birth date; month, day and datetime removed |
| Age top-coding | anyone aged 90 or over at the reference date (`deid_reference_date`, fixed at 2026-01-01 so rebuilds are reproducible) gets the year that makes them exactly 90 |
| Identifiers and free text | `person_source_value` removed; free-text observation values removed. Codes (SNOMED, LOINC, RxNorm) are kept |
| Not included | location, provider and care site are not populated in either tier |

**The key.** `DIP_DEID_KEY` (at least 32 characters) is generated by Terraform into the
`dip-deid` Kubernetes Secret and injected into Airflow pods; locally it goes in `.env`.
`python -m dip deid-key-install` (the DAG does this on every run) stores it in
`ops_secret.deid_key`, and the dbt macros read it inside SQL. It is sent to Postgres as
`COPY` data into a temporary table, never as statement text or a bind parameter, so it
never appears in compiled SQL, dbt logs, statement logs (`log_statement = all`) or
`pg_stat_activity`; only a fingerprint is logged. Superusers can still read
`ops_secret.deid_key` itself. Rotating the key changes every
pseudonym and shift, so extracts are only comparable when they share a key fingerprint
(R5 records it).

**Access control** is enforced by Postgres, not by convention. dbt grants `SELECT` on the
shareable tables and `USAGE` on the schema to `dip_researcher`, and nothing else. The role
is created in the cluster by `postgres-init.sh` on a fresh volume, and by the
`postgres-roles` Job (`infra/terraform/files/postgres-roles.sql`, idempotent), which
Terraform replaces and reruns on every `terraform apply`, so a cluster created before the
role existed, or one where it was dropped, gets it back on its next `scripts/up.sh`
without losing its data. (Every plan therefore shows that Job being replaced.)

**Tests**
- `dbt/tests/deid_privacy.sql` (runs in every `dbt build`): no restricted id reappears, no
  source identifier or free text, birth is a year only, ages 90+ are top-coded, every shift
  is 1–180 days, every person's observation period keeps its length, row counts match
  across tiers, **every** shareable date and timestamp equals the restricted value plus
  that person's shift (rows matched through their pseudonymous id), no column of any
  shareable table holds a restricted `person_source_value`, and `dip_researcher` has no
  write, `TRUNCATE` or `CREATE` privilege anywhere and can read nothing outside
  `omop_shareable` (`ops`, `ops_secret`, `vocab`, `dbt`, ...).
- `test_deid_privacy_catches_an_unshifted_date`: moving one shareable date by a day fails
  `deid_privacy`.
- `test_key_is_not_in_statement_logs_or_pg_stat_activity`: with `log_statement = all`,
  `log_min_duration_statement = 0` and full parameter logging, installing the key logs
  statements but never the key, and no `pg_stat_activity` row contains it (also checked
  for the psycopg2 path Airflow uses).
- `test_shareable_tier_shifts_dates_and_keeps_intervals`: the fixture patient's HbA1c
  dates move by exactly their shift, with values unchanged.
- `test_pseudonyms_depend_on_the_key`: same key → same pseudonym on rebuild; new key →
  new pseudonym.
- `test_researcher_role_reads_only_the_shareable_tier`: as `dip_researcher`, reading
  `omop_shareable` works, while `omop`, `deid`, `ops_secret`, `ops`, `vocab`, `bronze`
  and `silver` raise *permission denied*, and so do `INSERT`, `UPDATE`, `DELETE`,
  `TRUNCATE`, `CREATE TABLE` and `CREATE SCHEMA`.
- `test_no_key_fails_clearly`: no installed key → the build stops with a clear message.

> **Limits, stated plainly.** This demonstrates de-identification methods on synthetic
> data. It is **not** a certified HIPAA Safe Harbor or Expert Determination process: it has
> no small-cell suppression or k-anonymity analysis, and race, ethnicity and gender are kept.
> A real deployment would need a privacy officer's review and a key held in a KMS.

## R5. Versioned, reproducible research extracts

```bash
python -m dip extract build  --version 1.0.0     # writes data/extracts/1.0.0/
python -m dip extract verify --version 1.0.0     # dbt build again, regenerate, diff byte for byte
```

```
data/extracts/1.0.0/
├── data/person.csv, observation_period.csv, visit_occurrence.csv,
│        condition_occurrence.csv, drug_exposure.csv, measurement.csv, observation.csv,
│        procedure_occurrence.csv
├── datapackage.json     Frictionless Data Package: fields and types, primary keys,
│                        bytes and sha256 per file, license, keywords, provenance
└── manifest.sha256      sha256 of every file (sha256sum -c compatible)
```

**Provenance** recorded in `datapackage.json`: number of input files and a digest of their
sha256s, contract versions, the code's git commit, the vocabulary shard's commit and
checksum, the de-identification key **fingerprint** (never the key), and the CDM version.

**Deterministic by construction**: rows are ordered by primary key, Postgres formats every
value as text, JSON keys are sorted, and nothing in the package is a timestamp. Two builds
from the same raw files, code and key are byte-identical, even when the files arrive at
different times.

**Immutable versions**: building a version that exists either reproduces it exactly (and
reports that) or fails with "published versions are immutable, choose a new version".

**FAIR, item by item**
| | How |
|---|---|
| Findable | versioned package name + version, keywords, a descriptor at a fixed path |
| Accessible | plain CSV + JSON, no proprietary formats; access governed by the shareable tier (R4) |
| Interoperable | OMOP CDM 5.4 tables with standard OHDSI concept ids; Frictionless Table Schema |
| Reusable | license, provenance, column types and keys, checksums |

The license follows the sources, which the extract detects itself: `CC0-1.0` by default
for synthetic (Synthea) data, where `--license` may choose another; **ODbL-1.0, required,
with attribution** for anything derived from the MIMIC-IV demos, where CC0 is refused. See
[mimic.md](mimic.md#licensing-of-outputs).

**What the test caught.** Its first run failed: the two builds differed. The Synthea script
pinned the seed and *reference* date but not the simulation *end* date, which defaults to
today. So patients had events up to the day of generation, and the intake's future-date rule
rejected different records depending on when the files "arrived". The end date is now
pinned too (`-e`, default 2026-01-01), which also makes the generator itself reproducible
from day to day. Arrival time still matters by design for genuinely future-dated records,
so a replay of raw files should use the recorded `ops.intake_file.received_at`.

**Across machines, two more causes.** Builds were byte-identical on one machine (two Synthea
runs compared file by file), but the Synthea input digest still differed between the Linux
CI runner and a Windows laptop. Two causes, found one at a time by comparing the
`extract inputs sha256` that the test prints:
1. the Java image was a floating tag (`eclipse-temurin:21-jre`), so the machines ran
   different Java builds. Pinning it by digest changed CI's output;
2. Synthea generates in parallel with one thread per core (CI has 4, the laptop 8), and the
   output depends on the thread count. With `--generate.thread_pool_size 1`, the laptop
   reproduces CI's digest exactly (`8935f4d0…`).

With both pinned, the same commit generates the same patients on CI and on the laptop.

**Test** (`tests/integration/test_extract.py`): the **whole pipeline runs twice from scratch**
(fresh warehouse, same raw files, arrival dates two months apart): intake → vocabulary →
dbt build → extract. The two extracts must have zero differing files. It also checks that the
manifest matches the files, that the descriptor carries provenance and no key, and that
rebuilding a published version with changed data is refused.

## R6. Lineage

Every layer emits [OpenLineage](https://openlineage.io) run events (`dip/lineage.py`), named
so they join into one graph:

| Job | Inputs | Outputs | Extra facets |
|---|---|---|---|
| `intake.<feed>` (per file) | `s3://landing/<file>` | `s3://raw/<copy>`, `postgres://…/warehouse.bronze.fhir_resource` (if anything was accepted), `s3://quarantine/<object>` | `dip_intake`: status, counts, reason codes, sha256 |
| `dbt.<model>` (per model/seed) | upstream models, seeds and sources | the model's table | `schema` (documented columns), `documentation`, `dataQualityAssertions` (that model's dbt test results, including the DQD-style checks) |
| `extract.omop_shareable` | `warehouse.omop_shareable.*` | `file/extracts/<version>/data/<table>.csv` | `dip_extract`: version, manifest sha256 |

**Transport**: `OPENLINEAGE_URL` (HTTP, for example Marquez) and/or `DIP_LINEAGE_FILE`
(NDJSON). With neither set, emitting does nothing. Marquez itself is deployed in Stage 3
(the `observability` profile), and the Airflow OpenLineage provider will be pointed at it
then; until then the cluster emits nothing. Today's events are verified with the file
transport.

**Why not `dbt-ol`**: openlineage-dbt depends on openlineage-sql, which has no wheel for
Windows/Python 3.11 here and would need a Rust toolchain. The dbt events are built from
dbt's own artifacts (`manifest.json`, `run_results.json`), which is also what `dbt-ol`
reads, so there is no extra dependency.

**Test** (`tests/integration/test_lineage.py`): runs intake → dbt → extract with a file
transport and walks the graph. There must be a path from
`s3://landing/…/good.json` through `bronze.fhir_resource`, `omop.measurement` and
`omop_shareable.measurement` to `extracts/1.0.0/data/measurement.csv`. The bad file's
lineage must end in raw and quarantine only, never bronze. `dbt.measurement` must carry
its HbA1c plausibility result as a data-quality assertion.
