# MIMIC-IV demo sources

> **This is real, de-identified patient data, not synthetic.** The MIMIC-IV demos are a
> 100-patient subset of MIMIC-IV, de-identified records of patients admitted to the Beth
> Israel Deaconess Medical Center, published by PhysioNet as open access under the
> [Open Data Commons Open Database License v1.0](https://opendatacommons.org/licenses/odbl/1-0/)
> (ODbL). In this repo they are **used locally only**:
>
> - downloaded by `packs/mimic/fetch.sh` into `data/mimic/`, which is gitignored
> - never committed, never downloaded in CI, never used as test fixtures (CI uses Synthea,
>   and the MIMIC code paths are tested with hand-written synthetic fixtures in MIMIC's shape)
> - no MIMIC-derived row is published. The docs and PRs carry aggregate counts only.
>
> Any MIMIC-derived database that is ever shared publicly must be released under ODbL
> with attribution (share-alike), **never CC0**. Synthea-derived extracts stay CC0.

## Datasets

| Dataset | Version | DOI | License |
|---|---|---|---|
| MIMIC-IV Clinical Database Demo on FHIR | 2.1.0 | [10.13026/vphg-y548](https://doi.org/10.13026/vphg-y548) | ODbL v1.0 |
| MIMIC-IV Clinical Database Demo | 2.2 | [10.13026/dp1f-ex47](https://doi.org/10.13026/dp1f-ex47) | ODbL v1.0 |

Checksums of every file are pinned in `packs/mimic/*.sha256` (copied from PhysioNet's
`SHA256SUMS.txt`); `fetch.sh` refuses any file that does not match.

## Required citations

PhysioNet asks users of these datasets to cite the following.

**MIMIC-IV Clinical Database Demo on FHIR**

> Bennett, A., Ulrich, H., Wiedekopf, J., Szul, P., Grimes, J., & Johnson, A. (2025).
> MIMIC-IV Clinical Database Demo on FHIR (version 2.1.0). PhysioNet. RRID:SCR_007345.
> https://doi.org/10.13026/vphg-y548

and the original publication:

> Bennett AM, Ulrich H, van Damme P, Wiedekopf J, Johnson AE. MIMIC-IV on FHIR: converting
> a decade of in-patient data into an exchangeable, interoperable format. Journal of the
> American Medical Informatics Association. 2023 Apr 1;30(4):718-25.
> https://doi.org/10.1093/jamia/ocad002

**MIMIC-IV Clinical Database Demo**

> Johnson, A., Bulgarelli, L., Pollard, T., Horng, S., Celi, L. A., & Mark, R. (2023).
> MIMIC-IV Clinical Database Demo (version 2.2). PhysioNet. RRID:SCR_007345.
> https://doi.org/10.13026/dp1f-ex47

**PhysioNet** (the standard citation, for both):

> Pollard, T., Moody, B. E., Lehman, L., Gow, B., Fernandes, C., Xie, C., Johnson, A.,
> Mark, R. G., & Heldt, T. (2026). PhysioNet as a global platform for biomedical research.
> Nature Health. https://doi.org/10.1038/s44360-026-00096-z

**MIMIC-IV-ED Demo** (a parent project of the FHIR demo, whose page asks that its parents
be cited; the MIMIC-IV Clinical Database Demo above is the other parent):

> Johnson, A., Bulgarelli, L., Pollard, T., Celi, L. A., Horng, S., & Mark, R. (2023).
> MIMIC-IV-ED Demo (version 2.2). PhysioNet. RRID:SCR_007345.
> https://doi.org/10.13026/jzz5-vs76

## Download

```bash
bash packs/mimic/fetch.sh            # both, about 65 MB
bash packs/mimic/fetch.sh fhir       # FHIR demo only
bash packs/mimic/fetch.sh relational # relational demo only
```

## M1. The FHIR demo through the FHIR intake

```bash
bash packs/mimic/fetch.sh fhir
python packs/mimic/run_fhir_demo.py --dsn postgresql://…/mimic_fhir   # a separate, local database
```

`run_fhir_demo.py` runs three passes and writes a report of **aggregate counts** to
`data/mimic/reports/` (local). The numbers below are from that report. The scope is
patients, hospital/ED/ICU encounters, diagnoses, lab results and medication orders; ICU
charted events, microbiology, administrations and dispenses are out of scope for now.

### What breaks against Synthea

**Pass A: the export as delivered** (10 NDJSON files) against the Synthea contract: all
10 quarantined with `FILE.NAME_PATTERN`. A Bulk Data export is one gzipped NDJSON file per
resource type, not one Bundle per patient.

**Pass B: regrouped into one Bundle per patient** (by `dip.fhir_bulk`), same contract:
all 100 patients quarantined with `FHIR.PATIENT_INVALID:FHIR.FUTURE_DATE:Patient.birthDate`.
MIMIC shifts every date for de-identification, birth dates included (2030–2149).

**Pass B2: as B with only the Patient checks waived**, which shows what breaks record by
record (each record counts once, under its first violation):

| Synthea-contract check | MIMIC records failing | Why |
|---|---|---|
| `UNKNOWN_CODE_SYSTEM:Observation.code` | 107,727 (all labs) | lab items use MIMIC's `mimic-d-labitems` codes; no LOINC |
| `FUTURE_DATE:MedicationRequest.authoredOn` | 15,225 | dates shifted into 2110–2202 |
| `UNKNOWN_CODE_SYSTEM:Medication.code` | 5,332* | NDC and local drug names, not RxNorm |
| `MISSING_FIELD:Condition.onsetDateTime` | 5,051 (all diagnoses) | MIMIC diagnoses belong to a hospital stay and carry no date |
| `UNKNOWN_CODE_SYSTEM:MedicationRequest.code` | 2,327 | inline local medication names |
| `MISSING_FIELD:Medication.code.coding` | 1,287* | IV mixes carry ingredients only |
| `FUTURE_DATE:Encounter.period.start` | 637 (all encounters) | shifted dates |

\* Shared Medications are copied into every patient bundle that uses them, so they are
counted once per patient.

Other differences that do not show up as rejections, but would have produced wrong OMOP:

- **References** are `Patient/<id>` and `Encounter/<id>` across files, not `urn:uuid:` inside
  one bundle. The regrouping keeps them as delivered, and the reference check accepts both.
- **Encounters**: the hospital, ED and ICU encounter files share one profile URL and differ
  only in their identifier system (`encounter-hosp`, `-ed`, `-icu`). A hospital encounter's
  `class` carries the *admission type* (EMER, OBSENC, AMB, SS), and there is no IMP at all.
  Mapping by class, as for Synthea, would have made inpatient stays look like ER visits.
- **ICU stays** are `partOf` a hospital encounter. In OMOP they are `visit_detail` (not built
  yet), so they fold into their hospital visit. A first build that missed this produced 637
  visits instead of 497.
- **87% of medication orders** go through `medicationReference`, and some point at IV mixes
  with no code; a first build left 2,843 drug rows with neither a concept nor a source
  value (caught by `sourceValueCompleteness`).
- **ICD codes** come without the dot (`4019`); OMOP stores `401.9`.

### What changed to load it

| Change | Where |
|---|---|
| Bulk Data → one Bundle per patient, streamed through a per-patient spool so the export never has to fit in memory | `dip/fhir_bulk.py` |
| `date_window` contract option (dates inside a window rather than "not after arrival"), for sources with deliberately shifted dates | `dip/fhir.py`, `contract.schema.json` |
| A MIMIC contract: window 2000–2220, conditions need an encounter instead of an onset, MIMIC code systems, IV mixes allowed | `packs/mimic/contracts/fhir_demo.yaml` |
| A staging layer (`stg_*`): generic source code + vocabulary, so every source feeds the same OMOP models | `dbt/models/staging/` |
| MIMIC code-system URIs → ICD9CM / ICD10CM / NDC; ICD codes get their dot; diagnoses dated by their encounter | `dbt/macros`, `fhir_condition` |
| Visit type by identifier system when the source defines one; ICU encounters fold into their parent visit | `stg_encounter`, `fhir_concept_map` seed |

The refactor is behavior-neutral for Synthea: on the same 25 patients, the extract built
before and after it is byte-identical (manifest sha256 compared on this laptop).

### Pass C: the result

All 100 patient bundles load with the MIMIC contract, with **zero quarantined records**
(137,686 resources). dbt builds OMOP in its own local database:

| OMOP table | Rows | Unmapped (concept 0) |
|---|---|---|
| person | 100 | 0% |
| visit_occurrence | 497 (275 inpatient, 222 ER) | 0% |
| condition_occurrence | 5,051 | 100% |
| measurement | 107,727 | 100% |
| drug_exposure | 17,552 | 100% |
| observation | 0 | — |

**DQ checks that do not pass**, and why:

| Check | Result | Explanation |
|---|---|---|
| `standardConceptRecordCompleteness` (conditions, measurements, drugs) | warn, 100% unmapped (with the Eunomia shard) | the Eunomia shard has no ICD-9-CM, ICD-10-CM or NDC concepts, and MIMIC labs carry no LOINC. Codes are kept as source values. **Update (V1–V2):** with a full local Athena vocabulary and MIT-LCP's lab map, labs are 95.2% mapped and this check passes for measurements; conditions and drugs still wait for a single Athena download that includes ICD9CM, NDC, SNOMED and RxNorm together. See [vocabulary.md](vocabulary.md) |
| `plausibleValueLow/High` on year of birth | fail, all 100 | years of birth 2030–2149 are "in the future". The check is right to flag them; the cause is MIMIC's de-identification shift, not bad data. Age and interval analysis still works, because all of a patient's dates shift together |

Everything else passes on the real data: primary and foreign keys, domains,
`plausibleAfterBirth`, `plausibleStartBeforeEnd`, `withinVisitDates`, the
in-observation-period rule and the de-identification invariants. The HbA1c and vital-sign
value-range checks also "pass", but with the Eunomia shard **vacuously**: they are keyed to
LOINC concepts, and no MIMIC lab maps to one. With the lab map loaded (V2), they test real
rows: 117 HbA1c and 262 blood-glucose results, all in range ([vocabulary.md](vocabulary.md)).
Vital signs come from ICU chartevents, which are not loaded, so those checks still have no
MIMIC rows.

**Tests that run in CI** use a hand-written, synthetic export *in MIMIC's shape*
(`tests/fixtures/mimic_shaped/`: invented ids, codes, values and dates): regrouping,
determinism, the MIMIC contract, the date window, the as-delivered rejection
(`tests/unit/test_fhir_bulk.py`), and the OMOP mapping rules end to end
(`tests/integration/test_mimic_shaped_omop.py`).

## M2. The relational demo through a relational-tables-to-OMOP path

```bash
bash packs/mimic/fetch.sh relational
python packs/mimic/run_relational_demo.py --dsn postgresql://…/mimic_rel   # its own local database
```

```
hosp/*.csv.gz ──▶ dip.relational (per-table contract) ──▶ schema mimic ──▶ dbt, source_kind=mimic_relational
                   header = contract columns?              patients           silver/mimic_*  ──▶ stg_* ──▶ omop.*
                   COPY into declared types?               admissions         (same staging contract and
                   primary key unique? not-nulls?          diagnoses_icd       the same OMOP models as FHIR)
                   any failure ─▶ table quarantined,       labevents
                   reason in ops.relational_load           prescriptions, d_labitems
```

**The contract** (`packs/mimic/relational/relational_demo.yaml`) names each table's file,
exact columns and types, primary key and not-null columns. Reason codes:
`SCHEMA.MISSING_COLUMN:<col>`, `SCHEMA.UNEXPECTED_COLUMN:<col>`, `SCHEMA.COLUMN_ORDER`,
`VALUE.TYPE_MISMATCH:<error>`, `KEY.DUPLICATE`, `VALUE.REQUIRED:<col>`. A failed table is
dropped rather than left half-valid.

**Mapping into the staging contract** (`dbt/models/mimic_relational/`):

| Staging | From | Rule |
|---|---|---|
| `stg_patient` | `patients`, `admissions.race` | birth date = Jan 1 of `anchor_year − anchor_age` (MIMIC has no birth date); race and ethnicity from the most recent admission's free text, mapped to OMB categories where the text allows |
| `stg_encounter` | `admissions` | every admission is inpatient (9201); `admission_type` is the source value |
| `stg_condition` | `diagnoses_icd` | ICD-9/10 with the dot; dated by the admission start |
| `stg_observation` | `labevents` | `itemid` as a LOCAL code; text-only results keep their text |
| `stg_medication_request` | `prescriptions` | NDC when present, else the drug name; the row has no natural key, so its id is a hash of the row |

Times in the relational tables are **local Boston time with no offset**. They are read as
`America/New_York` and stored in UTC, as the FHIR path does with its `-04:00/-05:00` offsets.

**Result (local, aggregates):** all 6 tables pass the contract. OMOP: 100 persons, 275
visits, 4,506 conditions, 107,727 measurements, 18,087 drug exposures. DQ: the same as the
FHIR path (100% unmapped conditions/labs/drugs against the shard; shifted years of birth),
plus one real source anomaly: **one prescription starts 8 days after its admission ended**
(a 4-day stay), which `withinVisitDates` flags.

**Tests in CI** (`tests/integration/test_mimic_relational.py`) use synthetic tables in the
demo's exact column layout (`tests/fixtures/mimic_shaped/make_relational.py`), describing the
same invented patient as the FHIR-shaped fixture: OMOP values end to end (including the
Boston-to-UTC conversion), and quarantine for a renamed column, an extra column, a duplicate
key and a type mismatch.

## M3. Reconciliation: the same patients through both paths

```bash
python packs/mimic/run_reconcile.py --fhir-dsn postgresql://…/mimic_fhir --rel-dsn postgresql://…/mimic_rel
```

The FHIR demo is derived from the relational demo, so the two OMOP builds should describe
the same 100 patients. `dip/reconcile.py` compares them by the **source's own keys**
(`subject_id`, `hadm_id` or ED stay id, ICD code, lab item and time) carried through the
staging layer, never by OMOP ids (which are hashes of different ids on each side). Rows are
multisets. Every difference is classified by a rule in `run_reconcile.py`, and anything no
rule covers is counted as **unexplained**; the runner exits non-zero if any is. The report
holds aggregate counts and difference *descriptors* (for example "+5h" or "null on a"),
never values.

**Result (local, aggregates): 0 unexplained differences.**

| Domain | FHIR | Relational | Identical | Differences, all explained |
|---|---|---|---|---|
| person | 100 | 100 | 95 | 5: race (below) |
| visit | 497 | 275 | **275 of 275 admissions, start and end to the minute** | 222 only in FHIR: ED visits |
| condition | 5,051 | 4,506 | **4,506 of 4,506** | 545 only in FHIR: ED diagnoses |
| measurement | 107,727 | 107,727 | 103,320 | 4,395 unit; 12 value (below) |

**The explanations:**

1. **ED visits and diagnoses (222 + 545).** The FHIR demo was built from two parents: the
   MIMIC-IV demo *and* the MIMIC-IV-ED demo. The relational demo has no ED module. Every
   FHIR-only visit is an ER visit (9203), and every FHIR-only condition sits on one.
2. **Race (5 persons).** All five are Hispanic/Latino patients, whose MIMIC race field holds
   text such as "HISPANIC/LATINO - SALVADORAN". The FHIR export records race White plus
   ethnicity Hispanic. The relational mapping records ethnicity Hispanic and leaves race
   unknown (0), because the text does not state a race. Both are modeling choices; I kept
   the more conservative one.
3. **Units on text results (4,395 measurements).** Results with no numeric value (for
   example "NEG") are `valueString` in FHIR, which has no unit field, so the unit in
   `labevents.valueuom` has no FHIR equivalent. The runner **verifies** this rule on every run
   (all 4,395 have no numeric value on either side) instead of assuming it.
4. **Comparator results (12 measurements).** Values such as "<5": the FHIR export parses a
   number (a `valueQuantity` with a comparator), while the relational `valuenum` is empty.
   Verified by counts: the relational demo has exactly 12 comparator-plus-number texts with
   an empty `valuenum`.

**What the reconciliation fixed along the way:** 330 measurements differed because some
relational units are a single space, which the FHIR export treats as "no unit". The
relational model now trims blank units.

**What it confirms:**
- *Times.* Every admission and lab time matches to the minute, so the relational
  timestamps really are Boston local time, and converting them via `America/New_York`
  (DST included, even for shifted 22nd-century dates) agrees with the FHIR offsets.
- *Birth years.* They agree: the FHIR export derives the year as `anchor_year − anchor_age`,
  as the relational path does.
- *ICU stays.* Folding them into the hospital visit is what makes the lab results match:
  each ICU lab result's visit key equals its admission.

**Drugs are not reconciled row by row**, because the two paths deliberately use different
source tables. The FHIR demo builds MedicationRequests from `pharmacy` (15,225, keyed by
`pharmacy_id`) plus 2,327 IV orders from POE. The relational path uses `prescriptions`
(18,087 rows, the table OHDSI's MIMIC-IV ETL uses). A row-level comparison would need a
pharmacy-to-prescriptions crosswalk, which is listed as a follow-up.

**Test in CI** (`tests/integration/test_reconcile.py`): both synthetic MIMIC-shaped fixtures
are built into two databases and reconciled. The reconciliation must match the patient,
the admission, both diagnoses and the ICU lab result (time-zone conversion included),
explain the ED visit, and report the one lab result that exists only in the relational
fixture as **unexplained**.

## Licensing of outputs

| Extract built from | License | Enforced by |
|---|---|---|
| Synthea (synthetic) | CC0-1.0 by default (`--license` may choose another) | `dip.extract.resolve_license` |
| Any MIMIC-IV source | **ODbL-1.0, required**, with the PhysioNet attributions in the package's `sources`, the description "REAL, de-identified", and `dip_distribution: local-only` | the same function: CC0 or any other license **raises an error** |

The extract finds its sources itself (intake feeds and relational source tables), so a
MIMIC-derived package cannot be mislabeled by forgetting a flag. MIMIC-derived extracts
are written under `data/` (gitignored) and stay local; only the aggregates on this page
are published.
