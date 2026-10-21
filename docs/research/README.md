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
