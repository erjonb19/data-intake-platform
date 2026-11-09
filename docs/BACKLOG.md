# Backlog

The source of truth for what gets built next. Work the first unchecked item in the order
given under "Order of work" below, which is not the order of the stage numbers. One PR per
item. If an item is blocked, write the reason under it and stop.

Every new or changed file in `docs/runbooks/` (the drill runbooks of Stages 13, 14 and 16,
and anything else added there) changes what the incident agent's runbook search returns, so
it means re-recording the incident cases **(ask)**. Each stage's incident write-up goes in
`docs/incidents.md` instead, which the agents never read.

Every stage also carries the five things in CLAUDE.md ("Built into every stage"): tests and
DQ checks in CI, tracing, alerts, agent evals with thresholds, and one break-and-fix incident
written into `docs/incidents.md`. Each stage below names its incident.

Anything marked **(ask)** needs the user's OK first: a system install on the laptop, a live
model call or new recording (CLAUDE.md, cost rule), or a change to a decision in PLAN.md.
Approved 2026-11-05: Haiku on the paid key ($5/month cap, cumulative spend logged in
`docs/PROGRESS.md` after any run that spends), Jaeger, metrics-server and
Alertmanager (each with an on/off switch in the Helm values, memory measured on the laptop),
Apache-2.0.

## Order of work (set by the user on 2026-11-08)

Stage and item numbers never change, because the README checklist and its test look items up
by number. Work the stages in this order instead:

1. **Stage 12** to the end: 12.5 to 12.8, then 12.9 and 12.10. Tag `v0.12.0` when Stage 12
   is done.
2. **Stage 14**, backups, restore and versioned migrations. These come up more in data
   engineering interviews than autoscaling does, and 14.3's migrations should exist before
   Stages 19 to 22 change the warehouse schemas.
3. **Stage 13**, deployment hardening. Its rollback goes back to a tagged image from 12.10.
4. **Stage 19**, proof at scale.
5. **Stage 16**, backpressure, webhooks, alert delivery and SLOs. The SLO targets come from
   Stage 19's measured numbers.
6. **Stage 20**, users and tenants. Its per-tenant quotas build on 16.1's backpressure.
7. **Stage 21**, contracts as a product.
8. **Stage 15**, Jaeger.
9. **Stage 22**, the connected platform and the member data lifecycle. 22.1 waits until the
   governed clinical agent's open PRs (#50 and #40 in that repo) are merged.
10. **Stage 23**, the showcase: a one-page architecture and a recorded walkthrough.

**Later:** Stages 17 and 18 are deferred. Re-decide them after Stage 22 and pick what still
earns its place. If 19.2's idempotent reruns need 18.5's reprocessing (F4), pull that item
forward and say so in its PR.

Every new service in these stages (Keycloak, an Iceberg catalog) is **(ask)** and falls
under the 12 GB memory rule in CLAUDE.md. Claims stay honest: the README says what was
measured and where (for example "tested at 1M members on a GitHub runner"), never
"enterprise scale".

## Done: Stages 0 to 11 (2026-10-17 to 2026-11-03)

Built and merged, PRs #1 to #104. Details in PLAN.md §8, demos in `docs/demos/`, and the
S11.12 audit in `docs/reports/audit.md` (109 items: 81 built, 20 partial, 8 missing; the
gaps are issues #83 to #101 and #103, all scheduled below).

- [x] **0. Foundations.** kind cluster with Terraform, Airflow (official Helm chart,
  KubernetesExecutor), Postgres, MinIO, CI. Demo: `docs/demos/stage-0.md`
- [x] **1. Thin slice + E2E.** Eligibility CSV, MinIO → contract → bronze/silver/gold, bad
  file to quarantine, kind E2E on every push. Demo: `stage-1.md`
- [x] **2. All channels and formats.** SFTP, intake API, Excel, Synthea claims, finance
  invoices, row-level quarantine, `FEED.LATE`, research track R1–R6, MIMIC-IV demos.
  Demo: `stage-2.md`
- [x] **3. OCR, monitoring, read-only MCP.** Scanned PDF invoices, Marquez lineage, Grafana
  and Prometheus alert rules, MCP Read tier with audit log. Demo: `stage-3.md`
- [x] **4. Breakage generator, runbooks 01–08, Propose tier.** No demo file yet (#86)
- [x] **5. Contract-author agent + eval harness.** No demo file yet (#87)
- [x] **6. Act tier + triage agent.** Demo: `stage-6.md`
- [x] **7. Incident agent + LangGraph handoff.** Demo: `stage-7.md`
- [x] **8. Hardening and write-up.** Demo: `stage-8.md`
- [x] **9. Agents as workloads + approvals page.** Demo: `stage-9.md`
- [x] **10. Multi-agent intake.** Demo: `stage-10.md`
- [x] **11. LLMOps and platform drills.** `ops.model_call`, traces, Agents dashboard,
  feedback loop, drills 09–12 (scheduler down, OOMKilled, bad secret, stuck queued),
  weekly live eval (schedule off). Demo: `stage-11.md`

Platform features already in place from those stages: Helm (Airflow, Prometheus, Grafana
charts) and Terraform for every service; readiness probes on most services; Prometheus and
Grafana with `promtool`-tested alert rules; Kubernetes Secrets (`airflow-connections`, the
`bad_secret` drill); Airflow's own migration Jobs and an upgrade CI job; platform drills
with runbooks 09–12; agent traces as rows (`ops.trace_span`) and lineage in Marquez.

## Stage 12: Green CI, honest README, small audit gaps

Delivers: everything red goes green, the README says what is true, and the cheap gaps from
the audit close. Demo on its own: the nightly evals run green in Actions, and the README has
a Production readiness checklist where every line links to its proof.
Incident: the nightly evals have been red since S11.5 (#83). Write-up in `docs/incidents.md`: what broke (a
hard-coded report count), how we knew (the nightly run failed), the fix.

- [x] **12.1 Nightly evals green** (#83): count the reports from the cases, not a constant
- [x] **12.2 The midnight-UTC E2E flake** (#101) and **the CronJob history limit** (#103).
  Green once the Airflow chart was committed (#108; the chart download had made every e2e
  run fail)
- [x] **12.3 README: "runs on Kubernetes locally" and the Production readiness checklist.**
  One line per feature with its file, CI job or demo; unbuilt ones listed as not yet built
- [x] **12.4 License file: Apache-2.0** (#84)
- [x] **12.5 `.wslconfig` guidance** (#85)
- [ ] **12.6 Demos for Stages 4 and 5** (#86, #87)
- [ ] **12.7 §8.9 status table refresh** (#100) and the README screenshot (#99)
- [ ] **12.8 PDF decompression-bomb check before inflating** (#97; a security fix)
- [ ] **12.9 Merge rules and supply-chain checks** **(ask)** before building.
  - Branch protection on `main`, requiring the `lint`, `test` and `e2e` checks (not
    `upgrade`, which runs only when certain paths change). The user turns this on in
    GitHub's settings; write the steps in the PR and check afterwards with
    `gh api repos/erjonb19/data-intake-platform/branches/main --jq .protected`.
  - Dependabot for pip, Docker and GitHub Actions, grouped into one weekly PR per ecosystem
    so the e2e runs stay few.
  - A Trivy scan of the images the repo builds (`images/`), in CI, failing on critical
    findings that have a fix; any accepted finding goes in `.trivyignore` with its reason.
  - CodeQL for Python and for the workflows.
  - Demo: a PR with a deliberately vulnerable pin fails the scan; a PR with a red `test`
    cannot be merged.
- [ ] **12.10 Versioned releases** **(ask)** before building. A `release` workflow that runs
  on a `v*` tag: builds the repo's images, pushes them to GHCR
  (`ghcr.io/erjonb19/...`) tagged with the version and the commit, and creates a GitHub
  release whose notes come from the `docs/PROGRESS.md` lines since the last tag. The kind
  cluster keeps building images locally; 13.2's rollback can deploy a released tag. First
  tag: `v0.12.0`, once 12.5 to 12.9 are done. Demo: the release page, its notes, and
  `docker pull` of the tagged image.

## Stage 13: Deployment hardening

Delivers: every long-running service has liveness and readiness probes, two services
autoscale, deployments roll without downtime, and a bad release rolls back with one command.
Features: liveness/readiness probes, autoscaling, rolling deployments and rollbacks,
OpenTofu.
Demo on its own: `dip drill bad_release` ships an image that fails its readiness probe; the
rollout stalls with the old pods still serving; `dip rollback intake-api` restores it; a load
script makes the intake API's HPA scale from 1 to 3 pods and back.
Incident: the bad release (runbook `13-bad-release.md`).

- [ ] **13.1 Liveness probes** on intake API, MCP, approvals, Marquez, Postgres, SFTP,
  Grafana (only MinIO has one today), with a test that every Deployment has both probes
- [ ] **13.2 Rolling update strategy** (`maxUnavailable: 0`) on every Deployment, and
  `dip rollback <service>` (`kubectl rollout undo` against `kind-dip` only)
- [ ] **13.3 The `bad_release` drill**, its alert, runbook 13 and an incident eval case.
  Prerequisite: the proof workflow's `gemini-recordings`, `claude-recordings` and
  `commit-results` jobs accept only `s11-*` branches; widen that to later stages' branches
  first, in its own PR
  (its recording **(ask)**)
- [ ] **13.4 Autoscaling:** metrics-server in kind, behind an on/off value in its Helm
  values, with its memory on the laptop measured and reported; HPAs for the intake API and
  MCP server; the scaling proof in the proof workflow
- [ ] **13.5 OpenTofu:** a CI job that runs `tofu init` and `tofu validate` on
  `infra/terraform/` so the code works under the open-source fork; README names both

## Stage 14: Backups and database migrations

Delivers: nightly Postgres backups to MinIO, a restore drill that proves they work, and
versioned warehouse migrations instead of DDL at startup.
Features: backups to MinIO with a restore drill, database migrations.
Demo on its own: `dip backup now` writes a dump to `s3://backups/`; `dip drill drop_table`
drops `gold.member_eligibility`; `dip restore --latest --verify` brings it back and the row
counts and checksums match the pre-drop snapshot.
Incident: the dropped table (runbook `14-restore.md`).

- [x] **14.1 Backup CronJob:** `pg_dump` of the warehouse to a MinIO bucket, retention of
  7 dumps, an `ops.backup` row per run, alert `BackupMissing` (none in 26 h)
- [ ] **14.2 Restore drill** in the proof workflow: drop, restore, verify counts and
  checksums, time-to-restore recorded
- [ ] **14.3 Versioned migrations** for the warehouse schemas (numbered SQL files with a
  `schema_version` table, or Alembic), run by a Job before the services start; the upgrade
  CI job applies them on a copy of the previous release's database

## Stage 15: Tracing with Jaeger

Delivers: every pipeline run and every agent step is a span in Jaeger, under one trace per
file or chain.
Features: Jaeger (with Prometheus and Grafana already in place).
Approved 2026-11-05, reversing PLAN §8.13 choice 4 ("rows, not an OpenTelemetry pipeline")
in part: the database trace rows (`ops.model_call`, `ops.audit_log`, `ops.trace_span`) stay
exactly as they are, and OTel is added as an export from them, so no recording fingerprint
changes. Jaeger all-in-one runs only in the observability profile.
Demo on its own: drop a file, open Jaeger, see intake → contract → dbt → (if quarantined)
incident → triage agent → model calls and tool calls as one trace.
Incident: a slow dbt model found from the trace (write-up in `docs/incidents.md`).

- [ ] **15.1 Jaeger all-in-one** in the observability profile, in-memory storage
- [ ] **15.2 Pipeline spans:** Airflow's OTel traces on, plus spans from intake and the
  contract check, keyed by file id
- [ ] **15.3 Agent spans:** an exporter that reads `ops.trace_span` and sends spans, so the
  agents and recordings stay unchanged
- [ ] **15.4 Grafana links** from a failed run or an incident to its trace

## Stage 16: Reliability: backpressure, webhooks, alert delivery

Delivers: a flood of files is throttled instead of piling up, vendors can notify us by
signed webhook, and alerts leave Prometheus and reach a receiver.
Features: backpressure, webhooks, Kubernetes Secrets (webhook signing keys), alerts.
Demo on its own: `dip drill flood` sends 500 files in a minute; the intake API returns 429
past its queue limit, Airflow pools cap concurrency, the backlog metric rises and drains,
nothing is lost. A signed webhook starts a feed's run; an unsigned one is rejected and
audited.
Incident: the flood (runbook `15-flood.md`).

- [ ] **16.1 Backpressure:** a queue-depth limit on the intake API (429 with
  `Retry-After`), per-feed Airflow pools and `max_active_runs`, a `dip_intake_backlog`
  metric and alert
- [ ] **16.2 Inbound webhooks:** HMAC-signed "file ready" endpoint on the intake API, the
  key in a Kubernetes Secret, replay protection, every call audited
- [ ] **16.3 Alertmanager**, behind an on/off value in the Helm values, its memory on the
  laptop measured and reported, with a webhook receiver (a small local sink
  that opens or annotates an `ops.incident`), so "a run fails, a contract breaks, an eval
  drops" each reaches a receiver; an alert on the nightly eval result
- [ ] **16.4 SLOs and error budgets.** Two or three service level objectives in
  `docs/slos.md`, with targets set from Stage 19's measured numbers, not guessed: the time
  from a file landing to its rows in gold (95th percentile), the intake API's availability,
  and each feed's quarantine rate. Prometheus recording rules, burn-rate alerts (fast and
  slow windows) tested with `promtool`, and an error-budget panel in Grafana.

## Stage 17: Chaos and on-call simulation

**Deferred on 2026-11-08** (see "Order of work"): re-decide after Stage 22.


Delivers: pods are killed on purpose and the platform recovers; an on-call drill runs from
alert to postmortem.
Features: chaos engineering, on-call simulation.
Demo on its own: `dip chaos kill --target postgres|mcp|intake-api|scheduler` deletes a pod;
the alert fires; the runbook is followed; recovery is checked; `docs/postmortems/` gets a
postmortem from a template (timeline from the audit log and trace, impact, root cause, fix,
follow-ups).
Incident: the on-call drill itself, with its postmortem.

- [ ] **17.1 `dip chaos kill`** (plain `kubectl delete pod`, `kind-dip` only; no Chaos
  Mesh unless asked), with recovery checks in the proof workflow
- [ ] **17.2 On-call drill:** an alert (Stage 16's receiver) opens an incident, the incident
  agent links the runbook, a person follows it, `dip postmortem <incident_id>` drafts the
  postmortem for a person to finish
- [ ] **17.3 Postmortem template** and the first two postmortems

## Stage 18: Open follow-ups from the audit

**Deferred on 2026-11-08** (see "Order of work"): re-decide after Stage 22.


Delivers: the remaining partial items from the S11.12 audit. Demo per item in its PR.

- [ ] **18.1 Runbooks 01–08 get Grafana and Marquez evidence** (#88)
- [ ] **18.2 S5.3: the missing contract-author drift cases** (#90)
- [ ] **18.3 Stage 6: a `release` action, and Act in the cluster beyond `request_resend`** (#89)
- [ ] **18.4 Lineage: per-feed bronze datasets** (#98) **and the shareable-extract leg** (#91)
- [ ] **18.5 OMOP mapping follow-ups F1–F3** (#93, #94, #95) **and F4 reprocess** (#96)
- [ ] **18.6 R7b: confirm the drift-monitor side** (#92; depends on the other repo)

## Stage 19: Proof at scale

Delivers: measured numbers for how much data the platform handles and how fast, and the first
bottleneck found and fixed. Today there is no load test, no benchmark, and no incremental dbt
model.
Features: volume generation, incremental models, partitioned tables, load testing, a
benchmark report.
Demo on its own: `dip generate --members 100000` writes vendor-sized eligibility and claims
drops; the `scale` job runs them through the platform; `docs/benchmarks.md` shows files per
hour, rows per second, the 95th-percentile time from landing to gold, and peak memory,
before and after the bottleneck fix.
Incident: the first bottleneck the load test finds (write-up in `docs/incidents.md`).

- [ ] **19.1 Volume generator:** synthetic eligibility and claims at 100k and 1M members,
  split into files the size real vendors send, built on the existing Synthea packs; a
  flood mode (many small files in a short time) that 16.1's `dip drill flood` reuses.
  Synthetic data only
- [ ] **19.2 Incremental dbt models and partitioned gold tables:** the large models
  (claims, eligibility) become incremental with unique keys; gold claims are partitioned
  by service month (Postgres declarative partitioning); a test that rerunning one day's
  files changes nothing (idempotent)
- [ ] **19.3 Load test and benchmark report:** a `scale` job in the proof workflow
  (`workflow_dispatch`, no model calls, $0) that runs 100k and then 1M members on a fresh
  kind cluster and records throughput, latency and peak memory; `docs/benchmarks.md` with
  the numbers, the machine they ran on, and the bottleneck found and fixed. 1M members runs
  on a GitHub runner only, never the laptop
- [ ] **19.4 Bronze as Iceberg tables on MinIO (optional)** **(ask)**: an Iceberg REST
  catalog, bronze written as Iceberg, read by dbt through DuckDB or Trino. Only if the
  memory rule allows, measured in the PR

## Stage 20: Users and tenants

Delivers: people log in with roles, healthcare and finance are separate tenants that cannot
see each other's data, and vendors have their own page. Today access is per-agent API keys
and one shared approvals login.
Features: login (OIDC), role-based access, tenant isolation, per-tenant quotas, a vendor
portal, human identity in the audit log.
Demo on its own: two vendor logins, one per tenant. One uploads a bad file, sees in plain
words why it was quarantined, and resends; the other vendor cannot see that file by any
path; a data steward approves the resend; `ops.audit_log` shows which person did each step.
Incident: the first cross-tenant read the isolation tests catch (write-up in
`docs/incidents.md`).

- [ ] **20.1 Login with Keycloak** **(ask: a new service)**: Keycloak in the cluster behind an
  on/off value, its memory measured and reported; an OIDC implementation of the
  `Authenticator` interface PLAN.md already describes, so the tools and the policy engine do
  not change. Agents keep their per-agent keys
- [ ] **20.2 Roles:** vendor uploader (own tenant's feeds only), data steward (approves),
  viewer, admin; enforced at the intake API, the approvals page and the MCP server, with a
  test that each role is refused outside its rights
- [ ] **20.3 Tenants:** the healthcare and finance packs become tenants, each with its own
  MinIO buckets, Postgres row-level security on every tenant-scoped table, and per-tenant
  quotas on the intake API (building on 16.1). A test tries to read tenant B as tenant A
  through every path: the API, the MCP tools and each SQL login
- [ ] **20.4 Vendor portal:** a page where a vendor logs in, uploads a file, sees its status
  and quarantine reasons in plain words, and resends
- [ ] **20.5 Human identity in the audit log:** every human action (upload, approve, reject,
  resend) records the person's identity beside the agent's

## Stage 21: Contracts as a product

Delivers: contracts carry versions, and a change that would break the dbt models built on a
contract fails in CI before it merges, including a change the contract-author agent
proposes.
Features: contract versioning, breaking-change detection.
Demo on its own: a PR that drops a column from the eligibility contract fails the check, which
names the dbt models it would break; bumping the major version and updating those models in
the same PR passes.
Incident: a breaking change the check catches (write-up in `docs/incidents.md`).

- [ ] **21.1 Versioned contracts:** a semantic `version` in every contract and a changelog per
  contract; a test that any change to a contract file changes its version; the version a file
  was checked against is recorded with the file
- [ ] **21.2 Breaking-change check in CI:** compare a PR's contracts with `main`'s and classify
  each change (a removed or renamed column, a narrowed type or a tightened rule is breaking;
  a new optional column is not); for a breaking change, use the dbt manifest to list the
  dependent models, and fail unless the major version is bumped and those models change in
  the same PR

## Stage 22: The connected platform and the member data lifecycle

Delivers: one story across two projects (a vendor file arrives, is checked and cleaned, and a
clinician asks a plain-English question about it through the governed clinical agent's PHI
guard), and a member's data can be deleted from every layer with an audit trail.
Features: a cross-project data product, retention and deletion.
Demo on its own: drop a healthcare file here, see its rows reach the governed agent's data
source, ask a question in the agent; then `dip forget --member <id>` and show the member is
gone from every layer, with the audit rows.
Incident: a copy of a member's data the first deletion run missed, found by its check
(write-up in `docs/incidents.md`).

- [ ] **22.1 Gold layer as the governed clinical agent's data source** **(ask: touches the
  other repo)**. Only after that repo's #50 and #40 are merged. A versioned, de-identified
  export of the healthcare gold layer (the shareable tier, under its PHI rules), a contract
  between the two repos, and an end-to-end check in one of them
- [ ] **22.2 Member data retention and deletion:** `dip forget --member <id>` removes a member
  from landing, bronze, silver, gold and quarantine (and any copy the export holds), with an
  `ops.audit_log` row; a retention period per layer with a scheduled purge; a test that no
  trace of the member remains

## Stage 23: Showcase

Delivers: what a hiring manager reads and watches in five minutes. PLAN.md is the design
record, but it is long.
Demo on its own: the page and the video.
No incident: this stage changes documentation only.

- [ ] **23.1 One-page architecture:** `docs/ARCHITECTURE.md` with one diagram, "how it scales
  and where it breaks" using Stage 19's numbers, and the main trade-offs, each linked to its
  PLAN.md decision
- [ ] **23.2 A three-minute walkthrough:** a script in `docs/demos/` for the user to record
  (file drop, quarantine, agent proposal, approval, gold, Grafana and the trace), linked from
  the README with a screenshot (overlaps #99 in 12.7; reuse it)
