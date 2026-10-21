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
| `standardConceptRecordCompleteness` (conditions, measurements, drugs) | warn, 100% unmapped | the Eunomia shard has no ICD-9-CM, ICD-10-CM or NDC concepts, and MIMIC labs carry no LOINC. Codes are kept as source values. A full Athena vocabulary would map conditions and drugs; labs would still need a MIMIC lab-item → LOINC map |
| `plausibleValueLow/High` on year of birth | fail, all 100 | years of birth 2030–2149 are "in the future". The check is right to flag them; the cause is MIMIC's de-identification shift, not bad data. Age and interval analysis still works, because all of a patient's dates shift together |

Everything else passes on the real data: primary and foreign keys, domains,
`plausibleAfterBirth`, `plausibleStartBeforeEnd`, `withinVisitDates`, the
in-observation-period rule and the de-identification invariants. The HbA1c and vital-sign
value-range checks also "pass", but **vacuously**: they are keyed to LOINC concepts, and no
MIMIC lab maps to one, so they check nothing here. Until labs are mapped, the MIMIC data
has no value-plausibility coverage.

**Tests that run in CI** use a hand-written, synthetic export *in MIMIC's shape*
(`tests/fixtures/mimic_shaped/`: invented ids, codes, values and dates): regrouping,
determinism, the MIMIC contract, the date window, the as-delivered rejection
(`tests/unit/test_fhir_bulk.py`), and the OMOP mapping rules end to end
(`tests/integration/test_mimic_shaped_omop.py`).
