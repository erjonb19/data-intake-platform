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
