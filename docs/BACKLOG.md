# Backlog

The source of truth for what gets built next. Work the first unchecked item. One PR per
item. If an item is blocked, write the reason under it and stop.

Every new or changed file in `docs/runbooks/` (the drill runbooks of Stages 13, 14 and 16,
and anything else added there) changes what the incident agent's runbook search returns, so
it means re-recording the incident cases **(ask)**. Each stage's incident write-up goes in
`docs/incidents.md` instead, which the agents never read.

Every stage also carries the five things in CLAUDE.md ("Built into every stage"): tests and
DQ checks in CI, tracing, alerts, agent evals with thresholds, and one break-and-fix incident
written into a runbook. Each stage below names its incident.

Anything marked **(ask)** needs the user's OK first: a system install on the laptop, a live
model call or new recording (CLAUDE.md, cost rule), or a change to a decision in PLAN.md.
Approved 2026-11-05: Haiku on the paid key ($5/month cap, cumulative spend logged in
`docs/PROGRESS.md` after any run that spends), Jaeger, metrics-server and
Alertmanager (each with an on/off switch in the Helm values, memory measured on the laptop),
Apache-2.0.

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
- [ ] **12.2 The midnight-UTC E2E flake** (#101) and **the CronJob history limit** (#103)
  CI on #107 (2026-11-06): both e2e runs failed before any test, at `helm_release.airflow`
  ("Error locating chart", airflow.apache.org unreachable). Unticked until CI is green.
- [ ] **12.3 README: "runs on Kubernetes locally" and the Production readiness checklist.**
  One line per feature with its file, CI job or demo; unbuilt ones listed as not yet built
- [ ] **12.4 License file: Apache-2.0** (#84)
- [ ] **12.5 `.wslconfig` guidance** (#85)
- [ ] **12.6 Demos for Stages 4 and 5** (#86, #87)
- [ ] **12.7 §8.9 status table refresh** (#100) and the README screenshot (#99)
- [ ] **12.8 PDF decompression-bomb check before inflating** (#97; a security fix)

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

- [ ] **14.1 Backup CronJob:** `pg_dump` of the warehouse to a MinIO bucket, retention of
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

## Stage 17: Chaos and on-call simulation

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

Delivers: the remaining partial items from the S11.12 audit. Demo per item in its PR.

- [ ] **18.1 Runbooks 01–08 get Grafana and Marquez evidence** (#88)
- [ ] **18.2 S5.3: the missing contract-author drift cases** (#90)
- [ ] **18.3 Stage 6: a `release` action, and Act in the cluster beyond `request_resend`** (#89)
- [ ] **18.4 Lineage: per-feed bronze datasets** (#98) **and the shareable-extract leg** (#91)
- [ ] **18.5 OMOP mapping follow-ups F1–F3** (#93, #94, #95) **and F4 reprocess** (#96)
- [ ] **18.6 R7b: confirm the drift-monitor side** (#92; depends on the other repo)
