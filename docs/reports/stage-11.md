# Stage 11 summary (2026-11-03): LLMOps and platform drills, done

The platform can now break itself on purpose, and the incident agent diagnoses the break from
Airflow logs and Kubernetes events. Every model call is recorded with its cost and its prompt
and model version, and a weekly live eval compares the agents against their recordings.
Each item was a PR of its own, merged in order after a fresh review with its findings fixed
and CI green:

- #68: the plan (§8.13)
- #69 to #75: S11.1 to S11.7
- #76 to #78: the workflow that commits recordings from the runner, and two fixes to its
  replay check
- #79 to #82: S11.8 to S11.11
- #102: S11.12, the audit

**A. Seeing what the agents do**

- **S11.1 Model calls.** `ops.model_call` has one row per model request: token counts, cost
  (from the same `cost_usd` the spend caps use), timing, model and prompt hash. It never
  holds prompt or response text. Replayed recordings write rows marked `recorded`.
- **S11.2 Trace.** `ops.trace_span` joins tool calls (`ops.audit_log`) and model calls into
  one timeline per chain. It is shown by `dip trace CHAIN_ID` and by a Trace button on the
  approvals page.
- **S11.3 Versions.** Every run and every proposal carries each step's model and prompt
  SHA-256. `dip agents versions` lists them.
- **S11.4 Dashboard and alerts.** There are six metrics from the exporter, a Grafana
  "Agents" dashboard, and three alerts with promtool tests:
  - `AgentFailureRateHigh`
  - `AgentFallbackRateHigh`
  - `ModelSpendDailyHigh`
- **S11.5 Feedback.** A rejection now needs a reason from a fixed list. `ops.agent_feedback`
  counts decisions per agent and week. `dip evals from-rejection ID` turns a rejected
  proposal into a candidate eval case, with every cell scrubbed to a synthetic value.

**B. Breaking the platform**

The `platform-check` CronJob watches the platform from outside Airflow, every 2 minutes. It
runs under its own service account, which has a namespace-scoped, read-only Role. It opens
and closes platform incidents. The incident agent got one new read tool,
`get_k8s_events`. Each drill is `dip drill <name> [--undo]`, runs on kind only, and has a
runbook, an alert and a recorded eval case.

| Drill | Item | What breaks | Incident | Runbook |
|---|---|---|---|---|
| `scheduler_down` | S11.6 (#74) | the scheduler is scaled to 0 | `PLATFORM.SCHEDULER_DOWN` | 09 |
| `oom_task` | S11.7 (#75) | a task allocates past its 128Mi limit | `TASK.OOMKILLED` | 10 |
| `bad_secret` | S11.8 (#79) | a wrong warehouse password in the connections Secret | `DAG.FAILED` | 11 |
| `stuck_queued` | S11.9 (#80) | a task asks for 256Gi, so its pod never schedules | `TASK.STUCK_QUEUED` | 12 |

**C. Proving it, off the laptop**

- **S11.10 The proof** (proof run 41611889836; record: `docs/demos/stage-11.md`).
  - Every drill ran on a GitHub runner with the incident agent live, and each was undone
    afterwards.
  - **Four of four** live diagnoses named the expected root cause, for **$0.0201**.
  - The same run's `fresh-clone` job followed the README's quick start from a clean
    checkout and passed the whole E2E. This closes Stage 8's open check.
- **S11.11 The weekly live eval** (proof run 41618947945). Eight sampled cases (two per
  agent) run live and are compared case by case with the recorded baseline. Result: **8 of
  8 pass, no drift, $0.0354 per run**. The cap is fixed at $0.10. The Monday schedule ships
  commented out; turning it on is the owner's call.
- **S11.12 The audit** (`docs/reports/audit.md`). A fresh agent, given only the repo and
  its CI, checked §1 and §8 to §8.13: **109 items, 81 built, 20 partial, 8 missing** (after a second review corrected the first count). Each
  gap has an open issue, #83 to #101.

**Found and fixed along the way**

- **Airflow 3.2.2 stores a task's exception beside the log event, not in it.** The task
  SDK 1.2.2 supervisor writes it as `error_detail`. `get_task_log` now reads it, so a
  refused login reaches the agent.
- **The provider version I had assumed was wrong.** The `apache/airflow:3.2.2` image ships
  the Kubernetes provider **10.17.1**, not the 10.25.0 I had read. The S11.9 E2E prints
  it. Both versions behave the same for a stuck queued task.
- **A freshly deployed stack could not record a scheduler outage.** The platform check
  failed with `permission denied for schema ops`, because the schema's grants were first
  applied by an agent run. A `warehouse-init` Terraform Job now applies them at deploy.
- **Recordings now reach the branch through the workflow, not a laptop.** This session
  could not download Actions artifacts. The proof workflow's `commit-results` job (#76)
  therefore commits recordings and proof results to the `s11-*` branch the run was
  started from. It holds no model key, never pushes to main, never force-pushes, and acts
  only after the scrub, the key and auth-header check, and the replay check pass.

**Known limitations** (each is an open issue; see the audit)

- **The nightly evals are red (#83).** `evals.yml` expects 6 reports, and S11.5 added a
  seventh. Every case passes; the count is stale.
- **The Stage 4 break-loop E2E check fails if a run crosses midnight UTC (#101).** The two
  files' business dates come from the day each one is received.
- **The E2E platform drill can lose its own check Job (#103).** The CronJob's history
  limit can delete the Job before the E2E reads it. This race was seen once, on #102.
- **Older gaps the audit surfaced:**
  - the §8.5 mapping follow-ups F1 to F4 (#93 to #96);
  - three §8.7 follow-ups (#91, #97, #98);
  - the missing Stage 4 and 5 demos (#86, #87);
  - runbooks 01 to 08 lack Grafana and Marquez evidence (#88);
  - Stage 6 `release` (#89);
  - no license file (#84).
- **The seeded drift is shown by replay, not live.** It is a test copy of a case with a
  changed expected answer. A live baseline has no latency, because recordings keep none.

**Spend (metered by `cost_usd`, the same meter the caps use):** about **$0.561 of the
$1.00** stage limit. Model calls were made only on GitHub runners, under
`DIP_SPEND_CAP_USD`. Every input was synthetic.

| What | Metered |
|---|---|
| S11.1 to S11.7, including their recordings | about $0.235 |
| S11.8 recordings (Claude run 41569344921) | $0.0657 |
| S11.9 recordings (Claude run 41588717652) | $0.0686 |
| Two earlier recording runs, superseded by re-recording | $0.1272 |
| S11.10 proof runs (two diagnostic runs, then 41611889836) | $0.0090 + $0.0201 |
| S11.11 live eval (41618947945) | $0.0354 |

The Gemini recordings ran on the free tier.

**Housekeeping:**

- The `ANTHROPIC_API_KEY` Actions secret is read only by the manual `proof` workflow.
- Every proof job tears its kind cluster down, so no cluster is left running.
- No private notes are in the repo.
