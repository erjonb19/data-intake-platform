# OMOP vocabulary: Eunomia shard vs full Athena (V1–V2)

CI maps codes through the public **OHDSI Eunomia** shard (about 2,300 concepts, Apache-2.0),
so CI results never depend on anyone's Athena account. Locally, a **full OHDSI Athena
download** can be loaded instead. Athena files are licensed to whoever downloaded them:
they are read from local folders, **never committed or published**, and extracts carry
concept ids only, never vocabulary rows (`scripts/check_no_mimic_data.sh` also fails CI if
an Athena file is ever tracked).

```bash
# Folders in load order; overlapping base vocabularies are de-duplicated (first wins).
python -m dip vocab-load --athena /path/athena_vocab /path/athena_vocab_extra
# or: DIP_ATHENA_DIRS="dir1;dir2" (os.pathsep-separated); the MIMIC runners use it too
python -m dip vocab-coverage     # unmapped rows and codes per OMOP table, counts only
```

`vocab-load` keeps `CONCEPT` and the "Maps to" rows of `CONCEPT_RELATIONSHIP`, records the
release in `vocab.load_info` (it goes into each extract's provenance), and warns when one
of the source vocabularies the pipeline maps from (ICD9CM, ICD10CM, ICD10, NDC) mostly has
no "Maps to" (see the cross-download gap below). Other vocabularies are not checked: many,
such as MeSH, never map.

## What was loaded (2026-10-21)

Two downloads of Athena release **v5.0 29-AUG-26**: SNOMED in one; Gender, LOINC 2.82,
RxNorm 20260601, RxNorm Extension, CVX and ICD10CM FY2027 in the other.
**4,190,579 concepts and 3,111,826 "Maps to" rows**, loaded in about 2 minutes.

## Unmapped rates, before (Eunomia) and after (Athena)

Rows whose standard concept is 0, per OMOP table. Counts only.

| Build | Table | Rows (before → after) | Unmapped before | Unmapped after |
|---|---|---:|---:|---:|
| Synthea | condition_occurrence | 788 → 491 | 392 (49.7%) | 12 (2.4%) |
| Synthea | observation | 5,655 → 5,980 | 0 | 6 (0.1%) |
| Synthea | measurement | 6,560 → 6,695 | 256 (3.9%) | 6 (0.1%) |
| Synthea | drug_exposure | 648 → 648 | 133 (20.5%) | 0 (0.0%) |
| MIMIC FHIR | condition_occurrence | 5,051 | 5,051 (100%) | 5,051 (100%) |
| MIMIC FHIR | measurement | 107,727 | 107,727 (100%) | **5,158 (4.8%)** |
| MIMIC FHIR | drug_exposure | 17,552 | 17,552 (100%) | 17,552 (100%) |
| MIMIC relational | condition_occurrence | 4,506 | 4,506 (100%) | 4,506 (100%) |
| MIMIC relational | measurement | 107,727 | 107,727 (100%) | **5,158 (4.8%)** |
| MIMIC relational | drug_exposure | 18,087 | 18,087 (100%) | 18,087 (100%) |

Synthea rows move between tables because a full SNOMED routes 297 more condition records
(social findings, "medication review due") to the Observation domain, as OMOP requires.
All of Synthea's unmapped rows together: **781 of 13,651 (5.7%) → 24 of 13,814 (0.2%)**.

### MIMIC labs: mapped by MIT-LCP's map (V2)

MIMIC lab items are MIMIC's own `itemid` codes. MIT-LCP publishes a map to LOINC in
[mimic-code](https://github.com/MIT-LCP/mimic-code) v2.4.0,
`mimic-iv/concepts/concept_map/d_labitems_to_loinc.csv` (**MIT license**; cited in the
README). It is fetched pinned by commit (`570ef019…`) and sha256 (`f7ef583b…`), never
committed, and loaded into OMOP's `vocab.source_to_concept_map` under the source
vocabulary `MIMIC_LAB`, which both MIMIC paths now use for lab codes.

- The map covers 1,400 of 1,630 items. One upstream row (itemid 52434) has its LOINC code
  in the concept-id column; it is skipped, not guessed.
- 447 of the 498 lab items in the demo are mapped, so **95.2% of lab rows** get a LOINC
  concept. The 51 unmapped items (5,158 rows) keep their itemid as the source value.
- With LOINC concepts in place, the DQ value-range checks finally have MIMIC rows to test:
  HbA1c (117 results, 4.4–13.9 %, unit %) and blood glucose (262 results, 50–461 mg/dL)
  pass. Before, these checks passed vacuously on MIMIC.
- Reconciliation is unchanged: 0 unexplained differences.

### MIMIC diagnoses and drugs: still unmapped, and why

1. **ICD-9-CM and NDC are not loaded yet.** About half of the demo's diagnoses are ICD-9-CM,
   and drugs are NDC or local names. They come with the next download.
2. **ICD10CM loaded without its mappings.** 98,142 of 98,403 ICD10CM concepts have no
   "Maps to". **Athena exports a relationship only when both of its concepts are in the
   same download.** ICD10CM's mappings point to SNOMED, which was in a different download,
   so they were left out of both. The loader now warns about this.

**The fix is a single Athena download** that selects SNOMED, ICD10CM, ICD9CM, ICD9Proc,
ICD10PCS, NDC, RxNorm, RxNorm Extension and LOINC together (plus anything else wanted).
NDC → RxNorm has the same gap: NDC and RxNorm must be in the same download. That download
arrived the next day; see below.

## The full bundle (2026-10-22)

One Athena download (`athena_vocab_full`, release **v5.0 29-AUG-26**, still local only,
never committed) with every vocabulary, including SNOMED, ICD9CM, ICD9Proc, ICD10CM,
ICD10PCS, NDC, RxNorm, RxNorm Extension, LOINC, CVX, Gender, Race and Ethnicity:
**10,167,185 concepts and 7,433,971 "Maps to" rows**. CPT4 is skipped: Athena ships it
separately (`CONCEPT_CPT4.csv` plus a tool that needs a UMLS key to fill in names), and the
loader reads only `CONCEPT.csv`. The loader's gap warning is silent for all of the source
vocabularies the pipeline maps from.

Unmapped rows, Eunomia → the two separate downloads → the full bundle:

| Build | Table | Eunomia | Two downloads | **Full bundle** |
|---|---|---:|---:|---:|
| Synthea | condition_occurrence | 49.7% | 2.4% | **0.8%** (4 of 484) |
| Synthea | observation | 0% | 0.1% | **0.1%** (6 of 5,987) |
| Synthea | measurement | 3.9% | 0.1% | **0.1%** (6 of 6,695) |
| Synthea | drug_exposure | 20.5% | 0% | **0%** (0 of 648) |
| MIMIC FHIR | condition_occurrence | 100% | 100% | **3.3%** (139 of 4,237) |
| MIMIC FHIR | observation | — | — | **0%** (0 of 814) |
| MIMIC FHIR | measurement | 100% | 4.8% | **4.8%** (5,158 of 107,727) |
| MIMIC FHIR | drug_exposure | 100% | 100% | **34.3%** (6,021 of 17,552) |
| MIMIC relational | condition_occurrence | 100% | 100% | **3.3%** (125 of 3,774) |
| MIMIC relational | observation | — | — | **0%** (0 of 732) |
| MIMIC relational | measurement | 100% | 4.8% | **4.8%** (5,158 of 107,727) |
| MIMIC relational | drug_exposure | 100% | 100% | **16.8%** (3,043 of 18,087) |

Synthea overall: **16 of 13,814 rows (0.1%)** unmapped. Counts only; no MIMIC row leaves
the laptop.

- **Diagnoses:** now that ICD9CM and ICD10CM map to SNOMED, 814 FHIR and 732 relational MIMIC
  diagnoses have Observation-domain targets (most often "History of event", "Long-term current
  use of insulin" or anticoagulants, "Not for resuscitation"). They move to
  `observation`, as OMOP requires: 4,237 + 814 = 5,051 and 3,774 + 732 = 4,506, so no row is
  lost.
- **Drugs:** almost every unmapped MIMIC drug row has no NDC at all. Its source value is a
  local drug name or an IV mix (5,882 of 6,021 on the FHIR path, 2,631 of 3,043 relational).
  Rows that carry an NDC map at 98.9% (FHIR) and 97.4% (relational). The FHIR export takes
  more of its orders from local names than the relational `prescriptions` table does, which
  explains the gap between 34.3% and 16.8%.
- **One more pipeline fix:** 9 NDC codes (in each MIMIC build) map to the **Device** domain
  (syringes and similar), which failed DQD `fkDomain` on `drug_exposure`. OMOP puts devices in
  `device_exposure`, which this build doesn't have, so they stay as drug rows with concept
  0 and their source concept, like the Episode diagnoses above.
- **Reconciliation** of the two MIMIC builds: still 0 unexplained differences.
- **DQ on MIMIC** now fails only the two known, explained checks: shifted years of birth,
  and one prescription that starts after its admission ends.

## What the full vocabulary found in the pipeline

Running against a full vocabulary surfaced four problems the shard had hidden. All are fixed:

| Finding | Fix |
|---|---|
| Ethnicity concepts (38003563, 38003564) are not in these Athena downloads (no Race or Ethnicity vocabulary was selected), so `fhir_concept_map` pointed at missing concepts | added to `omop_fixed_concepts`, like race; a concept the vocabulary has always wins |
| 22 Synthea urinalysis results got a `value_as_concept_id` in the Condition, Measurement or Observation domain (e.g. "Mucus in urine"); DQD `fkDomain` expects Meas Value | only Meas Value concepts are used; others are 0 and keep their code in `value_source_value` |
| 3 MIMIC diagnoses map to the **Episode** domain, which this build has no table for, and were silently dropped | rows in a domain without a table stay in `condition_occurrence` with concept 0 and their source concept |
| 6 unmapped Synthea Observations (imaging, survey) were dropped as well | `observation` now takes every Observation that `measurement` doesn't, so no source row is dropped |

A new test, `no_source_rows_dropped`, checks that every staged condition and observation
lands in exactly one OMOP table. The last two fixes change the Synthea extract in CI: same
inputs (`8935f4d0…`), new manifest **`de49ce4f…`** (was `2ee776b5…`), because the
previously dropped rows are now in it.
