# Working rules for this repository

## Model recordings

Replays are fingerprinted on what the agent sends the model (system prompt, tools, and
every tool result: runbook search hits, Kubernetes events, database rows). Any change to
those after a recording was made breaks its replay.

- **Record last.** Make recordings (`--record`, Claude or Gemini, incident, triage, demo)
  only after every review fix on the branch is applied. A fix that lands after recording
  means recording again.
- **The full replay suite passes in CI before merge.** A branch that adds or changes
  recordings is not merged until the CI `test` job, which replays every recording, is
  green on the branch's final commit.
- **Recordings reach the branch through the proof workflow.** Dispatch
  `gemini-recordings` or `claude-recordings` on an `s11-*` branch; its `commit-results`
  job commits the files to that branch only after the scrub, the key and auth-header check
  and the replay check pass. A push by the workflow's token starts no CI, so the branch's
  next push by a person (the decisions-log line, say) is the one CI must pass.

## Ground rules

- **The work queue is `docs/BACKLOG.md`.** Always work the first unchecked item. PLAN.md
  stays the design record and decisions log; when the two disagree, BACKLOG.md wins and
  the decision gets a line in PLAN.md's log.
- **One stage at a time, one PR per piece.** Tests must stay green.
- **Cost stays at $0, with one approved exception.** Open-source tools, free or near-free
  LLMs. Ask before adding anything that could bill.
  - `claude-haiku-5-5` on the paid key is approved (2026-11-05) with a **$5 per calendar
    month cap**, counted by the `cost_usd` meter (it errs high).
  - CI replays recordings by default and never calls a model.
  - **Every new recording and every live model call needs the user's OK first**, Claude or
    Gemini, and runs under `DIP_SPEND_CAP_USD`.
  - After any run that spends, update the cumulative spend in `docs/PROGRESS.md`.
- **Approved by the user on 2026-11-05** (also in PLAN.md's decisions log):
  - Jaeger, added as an OpenTelemetry export (Stage 15). The database trace rows
    (`ops.model_call`, `ops.audit_log`, `ops.trace_span`) stay exactly as they are.
  - metrics-server and Alertmanager, each behind an on/off switch in its Helm values, with
    its memory use on the laptop measured and reported.
  - The license is Apache-2.0.
- **Agent guardrails.** Read-only by default, human approval before any change, an audit
  log of every agent action, and evals of agent behavior.
- **Synthetic and public data only.** Never Montefiore data.
- **The README stays honest.** The cluster is local, so it says "runs on Kubernetes
  locally" unless a piece moves to a cloud host.
- **If blocked,** write the reason under the item in `docs/BACKLOG.md` and stop.

### Built into every stage

1. Tests and data quality checks in CI on every push.
2. Tracing for every pipeline run and every agent step.
3. Alerts when a run fails, a contract breaks, or an agent eval drops.
4. Agent evals in CI with pass and fail thresholds (the agentic layer exists).
5. One real break-and-fix incident per stage, written into the runbook: what broke, how we
   knew, how we fixed it.

### When an item is done

- Tick it in `docs/BACKLOG.md` and add its line to `docs/PROGRESS.md` (what changed, how
  to check it).
- Tell the user in plain words what it does and how to run the demo, as short numbered
  steps.
- Add it to the "Production readiness" checklist in the README.
