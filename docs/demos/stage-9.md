# Stage 9 demo: the chain runs on its own, and a person decides in a browser

One local run on 2026-10-30, on the 16 GB Windows laptop, from `main` after the six Stage 9
PRs (#50 S9.1, #52 S9.2, #51 S9.3, #55 S9.4, #53 S9.5, #54 S9.6) merged. It is the stage's
one kind proof; CI's kind E2E covers every Stage 9 item on every push.

```bash
export TF_VAR_profiles='["core", "observability"]'
export TF_VAR_anthropic_api_key=...        # from .env; the cluster's only paid key
bash scripts/up.sh && bash scripts/smoke.sh && bash scripts/e2e.sh
# port-forwards: Grafana 3000, Marquez UI 3001, the approvals page 8081
python scripts/screenshots.py docs/images
bash scripts/down.sh
```

| Step | Result |
|---|---|
| `up.sh` | PASS in 12 minutes (images cached) |
| `smoke.sh` | PASS |
| `e2e.sh` | FHIR intake and the OMOP build, then the file feeds (the FHIR stage printed PASS; the run stopped before the file-feed summary line), `feed_sla`, then `agent_chain`. It stopped at `agent_chain`'s own check, which then required that nothing be claimed: correct for CI (no key), wrong for a run with a key. Fixed in this PR (see below). The stages after it (observability, lineage, the MCP Read tier, the break loop, the approvals page) pass in CI's kind E2E |
| screenshots | the six in the README |
| `down.sh` | PASS: the cluster and its local Terraform state (which held the key) removed |

## What the chain did, unattended

With the key in the `dip-agent-chain` Secret, the `agent_chain` DAG ran the incident ->
triage chain as pods Airflow launched (one task per run, each its own pod with resource
limits), on the incidents the E2E's quarantined files opened. Read from the warehouse
before teardown:

| | |
|---|---|
| `ops.agent_run` | 7 incidents, all `done`, each under its own chain id; none `failed`, none left `running` |
| Diagnoses | 7, all `quarantined_records`; 4 with runbook steps for a person (S9.3) |
| Proposals | 12, all `request_resend`, all `pending`, each for a different quarantine group, across the FHIR, eligibility, claims, 837, 835, invoice and invoice-PDF feeds (S9.5 holds a group to one) |
| Executed | nothing: every proposal waits for a person |

The proposals' groups: `FHIR.NOT_A_BUNDLE`, `FHIR.PATIENT_INVALID:...Patient.birthDate`,
`SCHEMA.MISSING_COLUMN:member_id`, `VALUE.NOT_ALLOWED:plan_code`,
`SCHEMA.MISSING_COLUMN:paid_amount`, `X12.TRANSACTION:835`, `X12.ENVELOPE:SE01`,
`VALUE.TYPE:service_date`, `FILE.BAD_ROW_RATIO`, `SCHEMA.MISSING_SHEET:Invoices`,
`PDF.INVALID`, `OCR.LOW_CONFIDENCE`.

Cost: not metered in the cluster. At the recorded incident evals' rate (6 chains for
$0.035), 7 chains come to about $0.04. Every input was synthetic or invented.

## The screenshots

The six in the README's "What it looks like", taken by `scripts/screenshots.py` in a
separate headless Chrome:
- Airflow: the eligibility intake DAG and its runs;
- the 837 intake task's log, with two files quarantined and their reason codes;
- Grafana's Feeds dashboard;
- Marquez: `gold.member_eligibility` back to the landed files;
- **the approvals page** (S9.2) with proposal 3 open: its group, root cause, rationale and
  the message to the vendor. The token is pasted once (it is kept for the tab only);
- **the audit log** on the same page: one chain, call by call, under its chain id (S9.6),
  from `get_incident` through `search_runbooks` and `propose_runbook_steps` to the
  handoff and the triage agent's proposals.

## What the run found

- **The E2E's `agent_chain` check assumed CI's no-key setup.** With a key, claiming
  incidents is the point. It now fails only on an incident left `failed` (which is also
  what a chain started without a key would leave); `running` is not checked, since a
  scheduled run may still be working when it looks.
- **Memory.** Free memory was 3.2 GB at the start (the test Postgres stopped for the run).
  Available memory fell to about 500 to 700 MB while the stack ran, and never stayed under
  250 MB. Nothing was killed this time.
