# OMOP vocabulary: Eunomia shard vs full Athena

CI maps codes through the public **OHDSI Eunomia** shard (about 2,300 concepts, Apache-2.0),
so CI results never depend on anyone's Athena account. Locally, a **full OHDSI Athena
download** can be loaded instead. Athena files are licensed to whoever downloaded them:
they are read from local folders, **never committed or published**, and extracts carry
concept ids only, never vocabulary rows (`scripts/check_no_mimic_data.sh` also fails CI if
an Athena file is ever tracked). Inside a local warehouse, `omop_shareable.concept` is a copy
of the loaded vocabulary that the researcher role can read; it is never exported.

```bash
# Folders in load order; overlapping base vocabularies are de-duplicated (first wins).
python -m dip vocab-load --athena /path/athena_vocab_full
# or: DIP_ATHENA_DIRS="dir1;dir2" (os.pathsep-separated); the MIMIC runners use it too
python -m dip vocab-coverage     # unmapped rows and codes per OMOP table, counts only
```

`vocab-load` keeps `CONCEPT` and the "Maps to" rows of `CONCEPT_RELATIONSHIP`, records the
release in `vocab.load_info` (it goes into each extract's provenance), and warns when one
of the source vocabularies the pipeline maps from (ICD9CM, ICD10CM, ICD10, NDC) mostly has
no "Maps to" (see the cross-download gap below). Other vocabularies are not checked: many,
such as MeSH, never map.

## What is loaded locally (2026-10-22)

One Athena download (`athena_vocab_full`, release **v5.0 29-AUG-26**) with every vocabulary,
including SNOMED, ICD9CM, ICD9Proc, ICD10CM, ICD10PCS, NDC, RxNorm, RxNorm Extension, LOINC,
CVX, Gender, Race and Ethnicity: **10,167,185 concepts and 7,433,971 "Maps to" rows**. CPT4
is skipped: Athena ships it separately (`CONCEPT_CPT4.csv` plus a tool that needs a UMLS key
to fill in names), and the loader reads only `CONCEPT.csv`. The gap warning is silent for
every source vocabulary the pipeline maps from.

MIMIC lab items are mapped to LOINC by MIT-LCP's published map (below).

## Unmapped rates: Eunomia vs the full bundle

Rows whose standard concept is 0, per OMOP table, on the same code (this branch). Counts
only; no MIMIC row leaves the laptop. Synthea is the CI set: 28 patients (seed 42, pinned
release).

| Build | Table | Eunomia | **Full bundle** |
|---|---|---:|---:|
| Synthea | condition_occurrence | 392 of 787 (49.8%) | **4 of 483 (0.8%)** |
| Synthea | observation | 163 of 5,818 (2.8%) | **6 of 5,987 (0.1%)** |
| Synthea | measurement | 256 of 6,557 (3.9%) | **6 of 6,692 (0.1%)** |
| Synthea | drug_exposure | 133 of 647 (20.6%) | **0 of 647 (0%)** |
| MIMIC FHIR | condition_occurrence | 5,051 (100%) | **139 of 4,237 (3.3%)** |
| MIMIC FHIR | observation | 0 rows | **0 of 814 (0%)** |
| MIMIC FHIR | measurement | 107,727 (100%) | **5,158 of 107,727 (4.8%)** |
| MIMIC FHIR | drug_exposure | 17,552 (100%) | **6,021 of 17,552 (34.3%)** |
| MIMIC relational | condition_occurrence | 4,506 (100%) | **125 of 3,774 (3.3%)** |
| MIMIC relational | observation | 0 rows | **0 of 732 (0%)** |
| MIMIC relational | measurement | 107,727 (100%) | **5,158 of 107,727 (4.8%)** |
| MIMIC relational | drug_exposure | 18,087 (100%) | **3,043 of 18,087 (16.8%)** |

- **Synthea overall: 944 of 13,809 rows (6.8%) → 16 of 13,809 (0.1%).** Both builds have
  exactly 13,809 rows; they only move between tables, as OMOP's domain routing requires.
  A full SNOMED sends 304 condition records (social findings, "medication review due") to
  `observation`, and LOINC codes the shard lacks send 135 observation rows to `measurement`
  (5,818 + 304 − 135 = 5,987). The MIMIC "Eunomia" column is from the M1/M2 builds, before the lab
  map, when every MIMIC code was unmapped.
- **MIMIC diagnoses:** with ICD9CM and ICD10CM mapping to SNOMED, 814 FHIR and 732
  relational diagnoses have Observation-domain targets (most often "History of event",
  "Long-term current use of insulin" or anticoagulants, "Not for resuscitation"). They move
  to `observation`, as OMOP requires: 4,237 + 814 = 5,051 and 3,774 + 732 = 4,506, so no
  row is lost.
- **MIMIC drugs:** most unmapped drug rows have no NDC at all; their source value is a local
  drug name or an IV mix (5,882 of 6,021 on the FHIR path, 98%; 2,631 of 3,043 relational,
  86%). Rows that carry an NDC get a standard Drug concept at 98.8% (FHIR, 11,531 of
  11,670) and 97.3% (relational, 15,044 of 15,456). The FHIR export takes more of its
  orders from local names than the relational `prescriptions` table does, which explains
  most of the gap between 34.3% and 16.8%.
- **MIMIC labs:** 4.8% unmapped (see the lab map below).
- **Reconciliation** of the two MIMIC builds: still 0 unexplained differences.
- **DQ on MIMIC** fails only the two known, explained checks (shifted years of birth; one
  prescription that starts after its admission ends) and, on the FHIR path, warns that
  34.3% of drug rows are unmapped, above the 30% threshold, for the local drug names above.

### MIMIC labs: mapped by MIT-LCP's map

MIMIC lab items are MIMIC's own `itemid` codes. MIT-LCP publishes a map to LOINC in
[mimic-code](https://github.com/MIT-LCP/mimic-code) v2.4.0,
`mimic-iv/concepts/concept_map/d_labitems_to_loinc.csv` (**MIT license**; cited in the
README). It is fetched pinned by commit (`570ef019…`) and sha256 (`f7ef583b…`), never
committed, and loaded into OMOP's `vocab.source_to_concept_map` under the source
vocabulary `MIMIC_LAB`, which both MIMIC paths use for lab codes.

- The map covers 1,400 of 1,630 items. One upstream row (itemid 52434) has its LOINC code
  in the concept-id column; it is skipped, not guessed.
- 447 of the 498 lab items in the demo are mapped, so **95.2% of lab rows** get a LOINC
  concept. The 51 unmapped items (5,158 rows) keep their itemid as the source value.
- With LOINC concepts in place, the DQ value-range checks have MIMIC rows to test: HbA1c
  (117 results, 4.4–13.9 %, unit %) and blood glucose (262 results, 50–461 mg/dL) pass.
  With the shard alone these checks passed vacuously on MIMIC.

## History: the first load, two separate downloads (2026-10-21, superseded)

The first Athena load used two downloads of the same release (SNOMED in one; Gender,
LOINC 2.82, RxNorm 20260601, RxNorm Extension, CVX and ICD10CM FY2027 in the other;
4,190,579 concepts and 3,111,826 "Maps to" rows). It mapped Synthea well (about 0.2% of
rows unmapped, with one hand-written test patient included) and, with the lab map, MIMIC
labs, but **MIMIC diagnoses and drugs stayed 100% unmapped**:

1. ICD9CM and NDC weren't in either download.
2. ICD10CM loaded without its mappings: 98,142 of 98,403 ICD10CM concepts had no "Maps to".
   **Athena exports a relationship only when both of its concepts are in the same
   download.** ICD10CM's mappings point to SNOMED, which was in the other download, so they
   were left out of both. NDC → RxNorm has the same trap. The loader now warns about it.

The fix was the single bundle above.

## What the full vocabulary found in the pipeline

Running against a full vocabulary surfaced five problems the shard had hidden. All are
fixed and tested:

| Finding | Fix |
|---|---|
| Ethnicity concepts (38003563, 38003564) were not in the first two downloads (no Race or Ethnicity vocabulary was selected), so `fhir_concept_map` pointed at missing concepts | added to `omop_fixed_concepts`, like race; a concept the vocabulary has always wins |
| 22 Synthea urinalysis results got a `value_as_concept_id` in the Condition, Measurement or Observation domain (e.g. "Mucus in urine"); DQD `fkDomain` expects Meas Value | only Meas Value concepts are used; others are 0 and keep their code in `value_source_value` |
| 3 MIMIC diagnoses map to the **Episode** domain, which this build has no table for, and were silently dropped | rows in a domain without a table stay in `condition_occurrence` with concept 0 and their source concept |
| Unmapped Synthea Observations (imaging, survey) were dropped: 163 rows with the shard, 6 with full Athena | `observation` now takes every Observation that `measurement` doesn't, so no source row is dropped |
| 9 MIMIC drug rows per build (2 distinct NDC codes) map to the **Device** domain (syringes and similar), which failed DQD `fkDomain` on `drug_exposure` | OMOP puts devices in `device_exposure`, which this build doesn't have, so they stay drug rows with concept 0 and their source concept |

A test, `no_source_rows_dropped`, checks that every staged condition and observation lands
in exactly one OMOP table. The two dropped-row fixes changed the Synthea extract in CI:
same inputs (`8935f4d0…`), new manifest **`de49ce4f…`** (was `2ee776b5…`), because the
previously dropped rows are now in it.

`measurement` and `observation` split staged Observations with one shared expression
(`routes_to_measurement` in `dbt/macros/omop.sql`) that is never NULL. Before, an unmapped
Observation with **no category** failed both filters (`NULL in (...)` is NULL) and was
dropped. `test_observation_without_category_is_not_dropped` covers it.

## Mapping rules that lose detail (documented, not yet fixed)

- **Diagnoses whose standard concept is in the Measurement domain** (some ICD codes map to
  a lab finding) stay in `condition_occurrence` with concept 0 and their source concept,
  the same rule as Episode and Device. OMOP would put them in `measurement` with no
  value; this build doesn't, so a cohort built on the Measurement concept misses them.
  With the full bundle: **53 of 4,237 MIMIC FHIR diagnoses (1.3%) and 45 of 3,774
  relational (1.2%)**; none in Synthea.
- **Codes with two or more "Maps to" targets** (for example an ICD code that maps to two
  SNOMED conditions) get one row, mapped to the **lowest standard concept id**. OMOP
  would write one row per target. The choice is deterministic: every sort in
  `source_to_standard` ends on a unique key, for vocabulary codes and site-map rows alike
  (`test_two_maps_to_targets_resolve_to_the_lowest_concept`). Before, two site-map rows
  for one local code to two standard concepts could resolve either way.
  With the full bundle, codes with several targets cover **273 MIMIC FHIR diagnoses
  (6.4%) and 242 relational (6.4%)**, 13 of them per build with targets in different
  domains; 27 and 25 observation rows from diagnoses; and **84 drug rows** per build.
  Synthea has none. Counts are from the full-bundle builds of 2026-10-22, before these
  review fixes; the new tiebreak only decides cases that had no defined order before, so
  they are unlikely to move.
