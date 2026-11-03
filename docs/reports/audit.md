# Audit: PLAN.md §1 and §8 to §8.13 against the code and CI (S11.12)

- **Commit audited:** `c4a7a03258e2d5f6b8c09f70dc744d88f2c80425` (main after PR #82; branch `s11-12-audit`)
- **Date:** 2026-11-03
- **Auditor:** an independent review agent. It was given only the repo, its tests and its CI, and no conclusions from the build sessions.

## Summary

| Status | Count |
|---|---|
| built | 81 |
| partial | 20 |
| missing | 8 |
| **total** | **109** |

The core platform is built and covered by CI. That includes contract intake through medallion layers with quarantine, both packs, the research track, the MCP tiers with approvals, the agents and their recorded evals, and the Stage 11 LLMOps and drills.

The gaps fall into five groups:

1. **Nightly evals are red.** `evals.yml` checks for exactly 6 reports, and S11.5 added a seventh, so the nightly run fails even though every case passes.
2. **Follow-ups never built.** The §8.5 mapping follow-ups (F1 to F4) and the three §8.7 follow-ups were never started.
3. **Demos missing.** There is no stage-4 or stage-5 demo.
4. **Runbooks thin.** Runbooks 01 to 08 have no Grafana or Marquez evidence.
5. **Smaller items:**
   - Stage 6 has no `release` action, and in the cluster only `request_resend` completes.
   - The S5.3 drift cases are incomplete.
   - The extract-to-Marquez lineage leg is missing.
   - The repo has no license file.
   - The `.wslconfig` guidance is missing.

**Method.**

- Read PLAN.md §1 and §8 to §8.13. Treated PLAN's own "built" and decisions-log statements as claims to check, not as evidence.
- For each item, grepped and read the code, SQL, Terraform, workflows, eval cases and docs at this commit (file:line below).
- Ran the offline checks:
  - `ruff check .`: all checks passed.
  - `bash scripts/check_no_mimic_data.sh`: ok.
  - `python -m pytest -q -p no:cacheprovider tests/unit`: 485 passed, 4 skipped.
- Did not run the integration tests: Postgres on localhost:5433 did not answer. For them, CI's `test` job is the evidence (it runs `pytest` over unit, integration and dbt against a Postgres service).
- Read GitHub Actions runs (ci, e2e, upgrade, evals, proof) and their job logs, and merged PRs #1 to #82 with selected descriptions.
- Called no model API. Dispatched nothing.

**CI state at the audited commit.**

| Workflow | Run(s) | Result |
|---|---|---|
| ci and e2e on `c4a7a03` | 41638061225, 41638061282 | in progress when audited |
| ci on the previous main merge (#81, `422b873`) | 41632157711 | test and lint green |
| e2e on #81 | 41632157966 | green |
| PR #82 head checks | ci 41632217967, e2e 41632173838 / 41632217864 | all green |
| upgrade on PR #81 | 41623075311 | green |
| upgrade, last scheduled on main | 41481005939 (2026-11-02) | green |
| **nightly evals** | **41631832492** (2026-11-03, sha `2f0994b`) | **failed** |

The nightly evals log shows every case passing, then `7 reports (expected 6); failed: []`.

## §1 What we are building

| Item | Status | Evidence | Missing |
|---|---|---|---|
| §1 Free, open-source platform | partial | Public GitHub repo; `git ls-files` has 644 files and no LICENSE/COPYING; `pyproject.toml` declares no license | A license file, so "open-source" holds legally |
| §1 Files checked against a written contract, then bronze → silver → gold or quarantine with a machine-readable reason | built | `dip/contracts.py`, `dip/csvfeed.py`, `dip/intake.py`; dbt `dbt/models/{eligibility,claims,finance,x12,silver,omop}`; `tests/e2e/cases.yaml:17-187` (status, reason codes, gold rows per case); e2e 41632157966 | |
| §1 Operators see everything in lineage and dashboards | built | `dip/lineage.py:6-9`; `infra/terraform/marquez.tf`; `observability/grafana/{feeds,dag-runs,agents}.json`; `tests/e2e/lineage.py`, `tests/e2e/observability.py:70-72` (three dashboards) | Extract leg of lineage: see R6 |
| §1 Agents help write contracts, triage quarantine and handle incidents, never changing anything without a human approval | built | `agents/policies/*.yaml`; `dip/agents/registry.py:52` (`Denied`); `tests/integration/test_act_tier.py:44,57` (`test_the_server_refuses_what_no_person_approved_for_the_agent`, `test_denied_then_allowed`); `tests/integration/test_agents.py:105` | |
| §1 Healthcare pack: eligibility, claims, 837P, 835 (CSV, X12) from Synthea | built | `packs/healthcare/contracts/{eligibility,claims,claims_837,remittance_835}.yaml`; `packs/healthcare/generators/{claims.py,x12.py,synthea.sh}`; `tests/integration/test_x12_dbt.py` (`test_claims_and_remittance_reach_the_marts_and_balance`) | |
| §1 Finance pack: Excel, native PDF, scanned PDF (noise, skew, blur) | built | `packs/finance/generators/invoice_pdfs.py:100-101` (rotate, GaussianBlur, speckle); `dip/excelfeed.py`, `dip/pdffeed.py`; `cases.yaml:115-187`. Deviation: invoices use the stdlib RNG, not Faker (`invoices.py:5`) | |
| §1 Test and CI data synthetic; MIMIC local only, never committed | built | `scripts/check_no_mimic_data.sh` (ok locally; CI lint step "No MIMIC data committed", job 125904162838); `tests/unit/test_check_no_mimic_data.py:108` | |

## §8 Stages and dates (the stage table)

| Item | Status | Evidence | Missing |
|---|---|---|---|
| §8 Stage dates | partial | Merges by stage are all before their "done by" dates: Stage 0 #1 (10-22), Stages 1 to 3 #16 to #35 (10-22 to 10-23), Stage 4 #42 and #43 (10-23), Stage 5 #37, Stages 6 to 8 #44 to #47 (10-23), Stage 9 #50 to #56 (10-30), Stage 10 #58 to #64 (10-30 to 10-31), Stage 11 #69 to #82 (10-31 to 11-03) | Stage 8's fresh-clone check closed on 11-03 (S11.10), after its 10-28 date. Stage 11 (11-07) is still open: S11.12 |
| §8 Each stage ends with `docs/demos/stage-N.md` and a green E2E | partial | `docs/demos/stage-{0,1,2,3,6,7,8,9,10,11}.md` | `stage-4.md` and `stage-5.md` do not exist |
| Stage 0 Foundations | partial | `Makefile:6`; `infra/terraform/cluster.tf:13` (kind); `infra/helm-values/airflow.yaml:4` (KubernetesExecutor); `scripts/prereqs.sh`; `ci.yml` lint plus `e2e.yml` cluster job | `.wslconfig` guidance: nowhere in README, docs or scripts (only PLAN mentions it) |
| Stage 1 Thin slice + E2E on every push | built | #16, #17; `e2e.yml` `on: push, pull_request`; `cases.yaml:18-48` (eligibility good, bad, row-level) | |
| Stage 2 All channels and formats, both packs | built | See §8.6; e2e 41632157966 | |
| Stage 3 OCR + monitoring + read-only MCP | partial | See §8.7; `tests/e2e/mcp_read.py`, `lineage.py`, `observability.py` | Marquez lineage "FHIR file → OMOP → shareable extract" is not shown in the cluster (see R6) |
| Stage 4 Breakage generator + runbooks + Propose tier | partial | See S4.1 to S4.5; `docs/runbooks/01-08` | Runbooks 01 to 08 have no Marquez evidence, and Grafana only as a mention of "the Feeds dashboard" in 01 and 05 (no panel or query). No `docs/demos/stage-4.md` |
| Stage 4 Propose tier: `propose_operation`, `ops.approvals`, `dip approvals` | built | `dip/agents/tools.py:210` (`propose_operation`); `dip/sql/warehouse.sql:233` (`ops.approvals`); `dip/__main__.py:31` (`approvals list\|show\|approve\|reject\|execute`); `tests/integration/test_agents.py:105,120,184,313` (waits for a person, runs once, tamper-proof, expiry, one pending per group) | |
| Stage 5 Contract-author agent + eval harness | partial | See §8.8; #37 | `docs/demos/stage-5.md`; drift cases incomplete (S5.3) |
| Stage 5R Cohort service (R7) | partial | R7a: see §8.4 | R7b lives in `erjonb19/clinical-drift-monitor`, not audited here |
| Stage 6 Act tier + triage agent | partial | See S6.1 to S6.3; `test_act_tier.py:57` | No `release` operation (`dip/agents/approvals.py:44`: `request_resend, discard, replay, propose_contract_change`). In the cluster `dip_mcp` has only SELECT on `ops.quarantine` and `ops.intake_file` (`dip/sql/warehouse.sql:625,628`), so the agent can complete only `request_resend` there |
| Stage 7 Incident agent + LangGraph handoff | built | See S7.1 to S7.4; nightly incident replay 10/10 (log of 41631832492) | |
| Stage 8 Hardening and write-up | partial | README screenshots (`README.md:26-67`); `scripts/demo.sh`; §11 OAuth item; fresh-clone job 125835579275 green | Nightly evals failing (S8.1) |
| Stage 9 Agents as workloads + approvals page | built | See §8.11 | |
| Stage 10 Multi-agent intake | partial | See §8.12; proof 41159028750 | One README screenshot (S10.6) |
| Stage 11 LLMOps and platform drills | partial | See §8.13 | S11.12 is this PR; its gap issues are #83 to #101. CI on the audited main commit was still running when audited |

## §8.1 Research-data track

| Item | Status | Evidence | Missing |
|---|---|---|---|
| R1 FHIR bundles through intake, `fhir_bundle` contract, `bronze.fhir_resource`, reason codes | built | `dip/fhir.py:104,154` (`FHIR.MISSING_FIELD`, `FHIR.NOT_A_BUNDLE`); `tests/unit/test_fhir.py`; `tests/e2e/fhir_intake.py`; #3 | |
| R2 OMOP CDM 5.4 gold via Eunomia, concept 0 for unmapped | built | `dbt/models/omop/*.sql` (person, observation_period, visit, condition, measurement, drug, procedure); `tests/integration/test_omop_dbt.py:54-152` | |
| R3 DQD-named dbt tests into `ops.dq_results` | built | `dbt/models/omop/_omop.yml`; `test_omop_dbt.py:156,176` (`test_dq_results_recorded`, `test_dq_catches_implausible_hba1c`) | |
| R4 De-identified shareable tier, date shift, HMAC ids, role split | built | `dbt/macros/deid.sql:13,20`; `dbt/models/shareable/`; `test_omop_dbt.py:209,231,245,277` | |
| R5 Versioned extract, byte-for-byte rebuild check in CI | built | `dip/extract.py`; `dip/__main__.py:12`; `tests/integration/test_extract.py:64` (`test_two_independent_rebuilds_are_byte_identical`) | |
| R6 OpenLineage from Airflow and dbt to Marquez, raw FHIR → shareable extract | partial | `dip/lineage.py:235` (`extract_event`); `tests/integration/test_lineage.py:69` (`test_lineage_reaches_extract_from_landing_file`); `infra/terraform/airflow.tf:82` | Own emitter instead of the Airflow provider and `dbt-ol` (neither is in `images/airflow` or `pyproject.toml`). The extract leg reaches Marquez only on a manual `dip extract build` with `OPENLINEAGE_URL` set (`marquez.tf:4-6`). No DAG or E2E checks it in the cluster |
| R7 Cohort service over the shareable tier | partial | See §8.4 | R7b (other repo) |
| §8.1 rules: pinned Synthea, no LLM calls, Eunomia in CI | built | `packs/healthcare/generators/synthea.sh:24,27` (v4.0.0, temurin by digest, seed); `evals.yml` "No model key" step | |

## §8.2 MIMIC-IV demos

| Item | Status | Evidence | Missing |
|---|---|---|---|
| M0 `fetch.sh` with pinned SHA256SUMS, citations | built | `packs/mimic/fetch.sh`, `packs/mimic/*.sha256`; `README.md:206-214` | |
| M1 FHIR demo through intake, date window, MIMIC code systems, separate schema set | built | `packs/mimic/contracts/fhir_demo.yaml:8,32` (`date_window`); `dip/fhir_bulk.py:1-12`; `tests/integration/test_mimic_shaped_omop.py`. Deviation: NDJSON is regrouped into bundles by an adapter, not a `fhir_ndjson` contract format | |
| M2 Relational tables to OMOP | built | `packs/mimic/relational/relational_demo.yaml`; `dbt/models/mimic_relational/`; `tests/integration/test_mimic_relational.py:50,104` | |
| M3 Reconciliation of the two builds | built | `dip/reconcile.py`; `tests/integration/test_reconcile.py:84` | |
| §8.2 rules: never committed, ODbL, CC0 refused for MIMIC | built | `dip/extract.py:118-119`; `test_reconcile.py:107` (`test_mimic_derived_extract_is_odbl_with_attribution`); MIMIC check script | |

## §8.3 Full OMOP vocabulary

| Item | Status | Evidence | Missing |
|---|---|---|---|
| V1 `dip vocab-load --athena`, de-dup, `vocab.load_info`, unmapped report | built | `dip/__main__.py:6,643`; `tests/integration/test_vocab_athena.py:71`; `docs/research/vocabulary.md:35-41` | |
| V2 MIMIC lab → LOINC via mimic-code, `SOURCE_TO_CONCEPT_MAP` | built | `dip/vocab.py:82-86,317-321`; `test_mimic_shaped_omop.py:101` | |
| V3 One full Athena bundle, CPT4 skipped, rows kept with concept 0 | built | `dip/vocab.py` (`athena_vocab_full`); `vocabulary.md` rates table; #18 | |
| §8.3 rules: Athena never committed, concept ids only in extracts, CPT4 not loaded | built | `scripts/check_no_mimic_data.sh:17,33` (Athena files fail CI); `tests/unit/test_check_no_mimic_data.py:137` (`test_athena_files_fail_anywhere`); `dip/vocab.py:8`; `docs/research/vocabulary.md:28-29` (CPT4 skipped) | |

## §8.4 R7a (this repo)

| Item | Status | Evidence | Missing |
|---|---|---|---|
| R7a.1 `dip_cohort_reader` login: read-only, timeout, connection limit, password from Terraform | built | `infra/terraform/files/postgres-roles.sql:22-31`; `infra/terraform/secrets.tf:16`; `test_cohort_reader.py:151`. Not a member of `dip_researcher`, by decision (PLAN §12, 10-22) | |
| R7a.2 `omop_shareable.cdm_source` (`DIP-SYNTHEA`/`DIP-MIMIC`) | built | `dbt/models/shareable/share_cdm_source.sql`; `test_cohort_reader.py:97`; `test_mimic_relational.py:71` | |
| R7a.3 `procedure_occurrence` in OMOP and shareable | built | `dbt/models/omop/procedure_occurrence.sql`; `test_omop_dbt.py:106`; #20 | |
| R7a.4 Reader reads only the shareable tier and writes nothing | built | `test_cohort_reader.py:105,122` (CI log shows the permission-denied probes) | |
| R7b Postgres engine etc. in the drift-monitor repo | missing | `docs/research/cohort-service.md` lists the hand-over | Not in this repo, and that repo was not audited |

## §8.5 Follow-ups

| Item | Status | Evidence | Missing |
|---|---|---|---|
| F1 One row per "Maps to" target | missing | `dbt/models/silver/source_to_standard.sql:59-60` still keeps the lowest target id | All of F1 |
| F2 Domain routing everywhere | missing | `condition_occurrence.sql:1-8,28` routes only the Observation domain; `procedure_occurrence.sql:2-9` keeps other domains at concept 0; `docs/research/README.md:119` says "not yet" | Measurement-domain diagnoses, non-Procedure procedures, the `no_source_rows_dropped` extension |
| F3 MIMIC procedures | missing | `dbt/models/staging/stg_procedure.sql:2` ("procedures_icd ... not mapped yet") | `procedures_icd` and `MimicProcedure*` loading and reconciliation |
| F4 `dip reprocess --feed --below-version` | missing | No `reprocess` in `dip/__main__.py` or `dip/*.py` | The command |

## §8.6 Stage 2

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S2.1 Excel format (`FILE.INVALID_XLSX`, `SCHEMA.MISSING_SHEET`, macros refused) | built | `dip/excelfeed.py:19-27`; `tests/unit/test_excelfeed.py`; `cases.yaml:122-132`; #23 | |
| S2.2 SFTP channel (digest-pinned, key-only, `.done` or stable size) | built | `infra/terraform/variables.tf:79`; `sftp.tf:17-25` (`tls_private_key`); `dip/sftp_sync.py:10,39`; `cases.yaml:39-40,89-90`; #24 | |
| S2.3 Intake API (hashed keys, 401/403/413) | built | `dip/intake_api.py:13,66`; `tests/unit/test_intake_api.py:100`; `tests/e2e/run.py:222` (401, 403, 202); #25. One key per pack, with feed globs | |
| S2.4 Synthea claims feed, `gold.claim_line`, coverage DQ | built | `packs/healthcare/generators/claims.py:3`; `tests/integration/test_claims_dbt.py`; `cases.yaml:49-61`; #26 | |
| S2.5 Finance pack, Excel invoices to gold, breakages | built | `packs/finance/generators/invoices.py`; `dbt/models/finance/vendor_invoice.sql`; `test_finance_dbt.py`; `cases.yaml:126` (`invoices_breakages.xlsx`); #29 | |
| S2.6 Late files, `feed_sla` every 15 minutes, `FEED.LATE` | built | `dags/feed_sla.py:15`; `dip/sla.py:1`; `tests/integration/test_feed_sla.py`; `tests/e2e/run.py:302`; #30 | |
| S2.7 E2E for each channel and format, demo, upgrade green | built | `cases.yaml` (SFTP good and bad, API good, 401/403, xlsx good and bad, claims, invoices); `run.py:302` (late feed); `docs/demos/stage-2.md`; #31 | |

## §8.7 Stage 3

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S3.1 PDF native and scanned, OCR, `OCR.LOW_CONFIDENCE`, `gold.invoice_match` | built | `dip/pdffeed.py`; `images/ocr/`; `dbt/models/finance/invoice_match.sql`; `tests/unit/test_pdffeed.py:199`; `cases.yaml:144-184`; #32 | |
| S3.2 Observability profile, exporter, two dashboards, alert rules | built | `infra/terraform/observability.tf:3,108-120`; `dip/metrics.py`; `observability/prometheus/rules.yml:8-49`; promtool tests in the CI lint step; #33 | |
| S3.3 Marquez, file → gold | built | `infra/terraform/marquez.tf`; `tests/e2e/lineage.py`; #34 | |
| S3.4 Read tier deployed (full tool list, read-only policy) | built | `agents/policies/reader.yaml:13-24`; `dip/agents/read_tools.py:230-366`; `tests/integration/test_mcp_read.py`; #35 | |
| S3.5 E2E: PDFs, low-confidence, lineage, metrics, authenticated and unauthenticated MCP | built | `scripts/e2e.sh:71-105`; `tests/e2e/mcp_read.py`; `docs/demos/stage-3.md:35` | |
| S3 follow-up: detect a PDF decompression bomb before inflating | missing | `dip/pdffeed.py:34-35` documents the limit as open | A stream-length cap in the reader |
| S3 follow-up: extract DAG or E2E step so the extract leg reaches Marquez | missing | `dags/` has no extract DAG; `tests/e2e/lineage.py` has no extract check | The DAG or E2E step |
| S3 follow-up: per-feed bronze datasets in lineage | missing | `docs/observability.md:113-116`; `dip/lineage.py:130-132` (every file feed uses `bronze.csv_record`) | Per-feed dataset names |

## §8.8 Stage 5

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S5.1 `diff_schema`, `validate_contract`, `propose_contract_pr`, local-branch approval step | built | `dip/agents/author_tools.py`; `dip/agents/contract_pr.py:64`; `agents/policies/contract_author.yaml`; `tests/integration/test_contract_author.py` | |
| S5.2 `dip contracts author --mode onboard\|drift`, prompt rules | built | `dip/agents/contract_author.py`; `agents/prompts/contract_author.md`. Deviation: one prompt, not two files | |
| S5.3 Eval cases from the breakage generator (drift, onboarding, injection) | partial | `agents/evals/contract_author/cases.yaml:21-159` has 13 cases: drift for a new code, rename, two added columns, delimiter and a claims column; 2 mistakes; injection in a cell and in a header; 3 onboarding | Drift cases for a type change and a widened range (the delimiter case covers "a delimiter or encoding change"). The Excel-comment injection case (allowed as the second cut) |
| S5.4 Scoring per mode, loosening, injection, variance | built | `dip/agents/author_evals.py`; `agents/evals/contract_author/results/{recorded,variance}.json`; nightly log shows 13/13, loosening 0 | |
| S5.5 Gemini comparison, docs, demo, triage → contract-author handoff | partial | `agents/evals/contract_author/results/gemini.json` (8/12); `docs/agents/EVALS.md:197-206` | `docs/demos/stage-5.md` does not exist |

## §8.9 Status table

| Item | Status | Evidence | Missing |
|---|---|---|---|
| §8.9 Status against the plan, kept accurate | partial | `PLAN.md` §8.9 rows | Several rows say Built where this audit finds gaps:<ul><li>Stage 4: runbook evidence content</li><li>Stage 6: `release` and the in-cluster actions (noted there)</li><li>Stage 8: nightly evals red since #73</li></ul> |

## §8.10 Stages 4, 6, 7 and 8

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S4.1 Synthetic 837P and 835, deterministic | built | `packs/healthcare/generators/x12.py`; `tests/unit/test_x12feed.py` (`test_the_generator_is_deterministic_and_its_isa_is_fixed_width`); #42 | |
| S4.2 `x12` format, envelope reason codes, row checks | built | `dip/x12feed.py:27-31,119`; `cases.yaml:69-111` | |
| S4.3 Claims and remittance marts with DQ tests | built | `dbt/models/x12/*.sql`, `_x12.yml`; `tests/integration/test_x12_dbt.py` | |
| S4.4 E2E cases for both feeds, `x12_segment_lost` | built | `cases.yaml:85`; `dip/breakages.py:153` | |
| S4.5 Break-and-fix loop for every scenario in CI, one through kind | built | `tests/integration/test_break_loop.py:22,38`; `tests/e2e/break_loop.py`; `scripts/e2e.sh:110-111`; #43 | |
| S6.1 Triage may execute only a person-approved, unexpired, unchanged approval; denials audited | built | `agents/policies/triage.yaml:9,29-30`; `registry.py:52`; `test_act_tier.py:44,98,108,121`; #44 | |
| S6.2 `dip triage act` and the denied-then-allowed demo | built | `dip/__main__.py:22`; `test_act_tier.py:57`; `docs/demos/stage-6.md` | |
| S6.3 Act evals | built | `agents/evals/triage_act/cases.yaml:25-83` (11 cases); `results/{recorded,variance}.json`; `test_act_tier.py:78` | |
| S7.1 `FILE.QUARANTINED` and `DAG.FAILED` incidents | built | `dip/incidents.py:7-35`; `test_incident_chain.py:53,71` | |
| S7.2 Incident agent with its own key and policy, `get_incident`, `report_diagnosis` | built | `agents/policies/incident.yaml`; `dip/agents/incident_tools.py`; `warehouse.sql:146` | |
| S7.3 LangGraph graph, typed `HandoffToTriage`, one chain id | built | `dip/agents/graph.py:49-60,263,296`; `test_incident_chain.py:123,157` | |
| S7.4 Incident and handoff evals | built | `agents/evals/incident/cases.yaml:25-274` (10 cases); `results/{recorded,variance,gemini}.json` | |
| S8.1 Nightly evals in CI, no key | partial | `.github/workflows/evals.yml:12-111`. The run on 11-02 (41445238603) is green. The 11-03 run 41631832492 is red: all cases pass, but `evals.yml:111` requires `len(reports) == 6` and #73 added a seventh (`triage-from-rejections`, `evals.yml:73`) | Update the report count, so the nightly run is green |
| S8.2 Recorded demo `scripts/demo.sh` | built | `scripts/demo.sh`; `dip/demo.py`; `tests/integration/test_demo.py`; `docs/demos/stage-8.md` | |
| S8.3 README screenshots, OAuth backlog, FOR_REVIEWERS and EVALS | built | `README.md:26-67`; `PLAN.md` §11 OAuth; `docs/FOR_REVIEWERS.md`; `docs/agents/EVALS.md` | |

## §8.11 Stage 9

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S9.1 `agent_chain` DAG: pods with limits, claim table, no-key skip | built | `dags/agent_chain.py:34-41`; `dip/agents/chain_job.py:17,70-85`; `warehouse.sql:119`; `test_agent_chain_job.py` (`test_without_a_model_key_nothing_is_claimed_and_incidents_wait`); #50 | |
| S9.2 Approvals page and audit viewer (own login, Host check, reviewer header) | built | `dip/agents/gateway.py:14-20,127-230`; `infra/terraform/approvals.tf`; `test_gateway.py`; `tests/e2e/approvals_page.py`; #52 | |
| S9.3 `search_runbooks` (BM25) and `propose_runbook_steps` | built | `dip/agents/runbooks.py:4,115-134`; `incident_tools.py:158,170`; `test_runbook_steps.py`; #51 | |
| S9.4 Four more breakage scenarios | built | `dip/breakages.py:157-163`; `tests/unit/test_breakages_s94.py`; PLAN §11 backlog line; #55 | |
| S9.5 One pending proposal per group | built | `dip/agents/tools.py:219-224` (advisory lock); triage case `group_already_pending`; #53 | |
| S9.6 `result_sha256`, `approval_id`, `chain_id` on the audit log | built | `warehouse.sql:341-344`; `tests/unit/test_audit_columns.py`; #54 | |

## §8.12 Stage 10

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S10.1 Automatic Gemini fallback per step | built | `dip/agents/llm.py:277` (`Failover`); `tests/unit/test_failover.py:39-94`; `tests/integration/test_fallback_chain.py:42,66,91`; `agents/evals/{triage,incident}/results/gemini.json`; #58 | |
| S10.2 `ops.intake_job`, `_onboard` trigger, drift job from handoff | built | `warehouse.sql:508`; `dip/intake_jobs.py`; `test_intake_jobs.py`; `tests/e2e/intake_jobs.py:5-65`; #59 | |
| S10.3 dbt models generated from a contract, golden tests, safe `write_branch` | built | `dip/dbt_gen.py:186`; `tests/fixtures/dbt_gen/golden/` (purchase orders, lab results, pharmacy claims, provider roster); `test_dbt_gen.py:27,62,76,92`; `test_dbt_gen_build.py:22`; #60 | |
| S10.4 Orchestrator and three workers, server-enforced proposal rule | built | `dip/agents/intake_graph.py:155`; `warehouse.sql:550` (`ops.contract_candidate`); `dip/agents/author_tools.py:204-207` (no proposal without the validator's and the reviewer's pass); `test_intake_team.py:60,139`; 35/36 live (`docs/reports/stage-10.md`); nightly 13/13, 0 injected text reached the map worker; #61 | |
| S10.5 Reviewer agent and verdicts, 8 seeded candidates | built | `agents/evals/reviewer/{cases.yaml,candidates/}` (8 files); `results/{variance,gemini}.json`; `test_intake_team.py:247`; `test_review_evals.py`; #62 | |
| S10.6 In the cluster, and the proof | partial | `images/mcp/Dockerfile:14-26`; `infra/terraform/mcp.tf:54,303-305`; proof run 41159028750 (success); `docs/demos/stage-10.md` | The audit-trail screenshot (`docs/images/intake-audit.png`) is only in `stage-10.md`. The plan says "added to the README", and only `intake-proposal.png` is there |

## §8.13 Stage 11

| Item | Status | Evidence | Missing |
|---|---|---|---|
| S11.1 `ops.model_call`, sink, `ops.agent_run_cost` | built | `warehouse.sql:362-403`; `dip/agents/model_calls.py:24-36`; `tests/unit/test_model_calls.py:62-139`; `tests/integration/test_model_calls.py:36,49,70`; `tests/e2e/intake_jobs.py:155-164`; #69 | |
| S11.2 `ops.trace_span`, `dip trace`, page trace view | built | `warehouse.sql:470`; `dip/agents/trace.py:22,39`; `gateway.py:230`; `test_trace.py:43`; `tests/e2e/approvals_page.py:85-91`; #70 | |
| S11.3 Prompt and model versions on calls, runs and proposals | built | `tests/unit/test_versions.py:46-71`; `tests/integration/test_versions.py:40-96`; `dip/agents/approvals.html:180`; `dip/__main__.py:59,96`; #71 | |
| S11.4 Agents dashboard, six metrics, three alerts with promtool tests | built | `dip/metrics.py:15-22`; `observability/grafana/agents.json`; `rules.yml:98-120`; `rules_test.yml:90-140`; `tests/e2e/observability.py:70-79`; #72 | |
| S11.5 Rejection reasons, `ops.agent_feedback`, `from-rejection` scrubbed | built | `warehouse.sql:257-298,446`; `dip/agents/feedback.py:324`; `tests/unit/test_feedback_scrub.py`; `tests/integration/test_feedback.py:51-104`; `agents/evals/triage/from_rejections.yaml` with its recording; `test_agents.py:248`; #73 | |
| S11.6 `platform-check` CronJob (namespace Role), platform incidents, `get_k8s_events`, `scheduler_down` drill, alert, runbook 09, eval | built | `infra/terraform/platform_check.tf:15-137`; `dip/incidents.py:20-36`; `read_tools.py:285`; `dip/drills.py:54,59` (`scheduler_down` and its undo); `tests/e2e/platform_drill.py:325-357` (opens within 240 s, alert, undo); `test_platform_check.py`; incident recordings (Claude and Gemini); #74 | |
| S11.7 `oom_task` drill, `TASK.OOMKILLED`, alert, runbook 10, eval | built | `dags/drill_oom.py`; `drills.py:71`; `platform_drill.py:119-137`; `test_platform_check.py:199`; #75 | |
| S11.8 `bad_secret` drill, `DAG.FAILED` from the check, secret not logged, runbook 11, eval | built | `drills.py:123-140`; `platform_drill.py:200-267`; `test_platform_check.py:237`; `tests/unit/test_incident_score_secret.py`, `test_task_log_credentials.py`; #79 | |
| S11.9 `stuck_queued` drill, `TASK.STUCK_QUEUED`, alert, runbook 12, eval | built | `dags/drill_queue.py`; `drills.py:183`; `platform_drill.py:276-313`; `test_platform_check.py:295-382`; #80 | |
| S11.10 Proof: drills live and fresh-clone, one run | built | Proof run 41611889836: `drills` job 125835579750 and `fresh-clone` job 125835579275 both success; `docs/demos/stage-11.md:33-38` (4/4 root causes, $0.0201); §8.9 Stage 8 row Built; #81 | |
| S11.11 Weekly live eval, baseline, drift job, schedule off | built | `scripts/live_eval.py:39`; `proof.yml:516` (`live-eval`, `schedule:` commented at `proof.yml:22-23`); run 41618947945 job 125859408735 green; PR #82 body ($0.0354 per run); `test_live_eval.py:57,67` (seeded drift fails the job's command, by replay, a deviation the PR records) | |
| S11.12 The audit | partial | This file | The final main commit's CI was still running when audited. The gap issues (#83 to #101) were opened with this PR |

## Corrections after review

A second fresh review checked this report (coverage, about 40 citations, the gaps against the issues). Its findings are applied above:

- **The counts.** The first summary said 106 items with 79 built. The tables held 107 rows with 80 built: an escaped `|` hid the S5.2 row from the count.
- **Two rows added for items the first pass folded into others:** the Stage 4 Propose tier, and the §8.3 rules. That makes 109 rows.
- **Status changes:**
  - Stage 10 is now partial, because its own row names a gap (S10.6, #99), as the other stage rows do.
  - In S5.3, "encoding change" is dropped from the missing cases. The plan asks for "a delimiter or encoding change", and the delimiter case covers it. #90 is updated to match.
- **Fixes to wording and citations:**
  - The Stage 11 row is no longer stale about the issues.
  - The Stage 4 runbook wording is corrected.
  - Three weak citations are replaced: S10.4's proposal rule, `scheduler_down`, and `ops.model_call`.
- **§8 Stage dates** is the one partial row without an issue; gap 21 says why.
- **Not given rows of their own:** the design choices of §8.12 and §8.13, and the §8.8 rule on what data goes to model providers. They are checked through the items that implement them.

## Gaps

Each gap is written as a proposed issue: a title, then a one-sentence body.

1. **Nightly evals fail on a stale report count.** (#83) `evals.yml:111` expects exactly 6 reports, but S11.5 (#73) added `triage-from-rejections` as a seventh, so run 41631832492 failed with every case passing. Count the runs the step starts, or expect 7.
2. **Add a license file.** (#84) §1 promises an open-source platform, but the repo has no LICENSE and `pyproject.toml` declares none. Add one (for example Apache-2.0 or MIT) and its metadata.
3. **Add `.wslconfig` guidance (Stage 0).** (#85) Stage 0 promises `.wslconfig` guidance, but no doc or script has it. Add a WSL2 memory section to the README and a hint in `scripts/prereqs.sh`.
4. **Write `docs/demos/stage-4.md`.** (#86) §8 says each stage ends with a demo script, and Stage 4 (837/835, `dip break`, the break-and-fix loop) has none.
5. **Write `docs/demos/stage-5.md`.** (#87) S5.5 requires it: the contract author in onboard and drift modes, the Gemini comparison, and the triage → contract-author handoff end to end.
6. **Runbooks 01 to 08 need Grafana and Marquez evidence.** (#88) Stage 4 says each runbook has symptoms, Grafana, Marquez and MCP evidence. Today they have MCP and SQL evidence, Grafana appears only in 01 and 05, and Marquez in none. Add the dashboard panels and lineage views to each.
7. **Stage 6: a `release` action, and Act in the cluster beyond `request_resend`.** (#89) The Stage 6 row lists release, discard or replay. `release` is not an operation, and the in-cluster `dip_mcp` login can only read `ops.quarantine` and `ops.intake_file`, so the agent completes only `request_resend` there. Build `release` and give the executing path what discard and replay need, or amend the plan.
8. **S5.3: the missing contract-author drift cases.** (#90) Add the type-change and widened-range drift cases, and the Excel-comment injection case, that S5.3 lists.
9. **R6: the shareable-extract leg of lineage in Marquez.** (#91) Today the extract leg is emitted only by a manual `dip extract build`. Run it in the cluster (an extract DAG or an E2E step) and check the FHIR → OMOP → extract path in Marquez, as Stage 3 and R6 require. Either adopt the Airflow OpenLineage provider and `dbt-ol`, or record the own-emitter choice in the decisions log.
10. **R7b: confirm the drift-monitor side.** (#92) The Postgres engine, the Synthea-only `cdm_source` check, the `measurement_concept_ids` criterion and offline CI belong to `clinical-drift-monitor`. Track them there and link the result here, since this audit could not check them.
11. **F1: one OMOP row per "Maps to" target.** (#93) `source_to_standard.sql` keeps only the lowest target concept. Emit one row per target, with ids that stay unique (§8.5).
12. **F2: domain routing for diagnoses and procedures.** (#94) Route Measurement-domain diagnoses, and procedures whose standard concept is not in the Procedure domain, to their domain's table. Extend `no_source_rows_dropped` to prove nothing is lost (§8.5).
13. **F3: load MIMIC procedures.** (#95) Load `procedures_icd` on the relational path and `MimicProcedure*` on the FHIR path, then reconcile the two as M3 does for diagnoses (§8.5).
14. **F4: `dip reprocess --feed F --below-version N`.** (#96) Replay the raw copies of files ingested under an older contract version through the current contract, so contract upgrades such as Synthea v2's Procedure reach old files (§8.5).
15. **Detect a PDF decompression bomb before inflating.** (#97) `dip/pdffeed.py:34-35` notes that a stream inflating to gigabytes is bounded only by the pod's memory. Cap stream lengths in the reader so an OOM-killed task cannot leave the file to fail again (S3.1 follow-up).
16. **Per-feed bronze datasets in lineage.** (#98) Every CSV, Excel, PDF and X12 feed emits `bronze.csv_record`, so Marquez joins unrelated feeds' graphs. Name a per-feed bronze dataset (S3.3 follow-up).
17. **S10.6: the intake audit-trail screenshot in the README.** (#99) S10.6 says screenshots of the job's audit trail and the proposal go in the README. Add `docs/images/intake-audit.png` beside `intake-proposal.png`.
18. **§8.9: refresh the status table.** (#100) Update §8.9 so its Built claims match this audit: Stage 4 runbook content, Stage 6 `release`, and Stage 8 nightly evals. Also add Stages 9 to 11.
19. **Open the issues for this audit's gaps (S11.12).** S11.12's done-when requires an open issue for each partial or missing item. Done: issues #83 to #100 for gaps 1 to 18, and #101 for gap 20, each linked above.

## Also noted by the build session (not found by this audit)

The auditor did not find this item; it was seen during Stage 11 and is recorded here so it gets an issue too.

20. **The Stage 4 break-loop E2E check fails when a run crosses midnight UTC.** (#101) In e2e job 125629322267 (branch s11-9-stuck-queued, 95419a5), `tests/e2e/break_loop.py` uploaded `elig_missing_member_id.csv` at 2026-11-02T23:58:20Z and the resend `elig_resend.csv` (line 114) landed at about 00:03:40Z. Neither file name carries a date, so each file's business date fell back to the UTC day it was received; the days differed, so the quarantine entry stayed `open` and the check failed. The same check passed in another run at 23:39 the same night, and later e2e runs of that branch passed before it merged (#80). Make the check independent of the wall clock (dated file names, or a business date both files share).
21. **§8 Stage dates (no issue).** Stage 8 closed on 11-03 (S11.10's fresh-clone check), after its 10-28 date; Stage 11 is due 11-07 and closes with this PR. A missed date cannot be fixed afterwards, so this row has no issue; it is recorded here so the slip is on the record.
