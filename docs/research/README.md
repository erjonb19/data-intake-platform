# Research-data track

PLAN.md Â§8.1 describes the plan. This page documents what is built, item by item.

> **All data here is synthetic.** Patients come from [Synthea](https://github.com/synthetichealth/synthea)
> (v4.0.0, fixed seed) or from small hand-written fixtures in `tests/fixtures/fhir/`. No real
> patient data is used, stored or committed anywhere in this repo. The feed is still
> classified `restricted` and handled as if it were PHI, so the controls are realistic.

## R1. FHIR intake

```
Synthea (pinned jar, seed 42, ref date 2026-01-01)
   â”‚  one FHIR R4 transaction Bundle per patient
   â–¼
landing/healthcare.fhir_patient_bundle/<file>.json          (MinIO)
   â”‚  fhir_intake DAG â”€ dip.intake.process_file
   â”œâ”€â–¶ raw/<feed>/<date>/<sha256>.json                       immutable copy, written first
   â”œâ”€â–¶ contract check: packs/healthcare/contracts/fhir_patient_bundle.yaml
   â”‚     file level   â”€ fail â”€â–¶ quarantine/<feed>/<date>/<sha256>.json + .reason.json
   â”‚     record level â”€ fail â”€â–¶ quarantine/<feed>/<date>/<sha256>.rejected.ndjson
   â–¼
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
                          source_to_standard     code → concept          concept
vocab.concept, vocab.concept_relationship ◀── python -m dip vocab-load (Eunomia shard, pinned + sha256)
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
| `drug_concept_id` | RxNorm on the order, or on its `Medication` | "Maps to" standard; end date = start date (orders carry none) |
| `observation_period` | all of the above | one period per person, first to last event |
| `*_type_concept_id` | | 32817 (EHR) everywhere |

Unmapped codes keep `concept_id = 0` and their `*_source_value`, as OMOP requires. The
vocabulary is a **shard**, not full Athena, so the unmapped rate on real Synthea output is
higher than in production; CI prints it per table, and R3 turns it into thresholds.

Concepts the shard lacks (gender, race, the EHR type concept, concept 0) are in
`dbt/seeds/omop_fixed_concepts.csv`, labeled with where they come from.

**Not built yet:** OMOP `observation` (surveys, social history), `procedure_occurrence`,
`death`, `provider`, `care_site`, and drug/condition eras.

### Tests

`tests/integration/test_omop_dbt.py` runs `dbt build` (models plus PK, FK and not-null
tests) on the fixture patient plus 25 Synthea patients, then checks known values: the
fixture's type 2 diabetes is concept 201826, its three HbA1c results are concept 3004410
in % (8554), its visits are outpatient (9202), its metformin order is concept 40163924,
its observation period runs 2018-03-02 to 2025-08-18, and `person_id` is unchanged by a
rebuild.
