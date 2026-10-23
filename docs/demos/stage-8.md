# Stage 8 demo: the whole chain, recorded

`bash scripts/demo.sh` runs one breakage through the whole platform on a laptop:
- **No key, no model:** the agents' model turns replay from recordings
  (`agents/evals/demo/recordings/`). Everything else is the real code: the intake, the MCP
  server with both agents' keys and policies, the LangGraph graph, the approvals and the
  audit log.
- **No setup:** it starts a throwaway Postgres in Docker and removes it afterwards (or uses
  `DIP_EVAL_DSN`). It needs Python with the dev extras (`pip install -e ".[dev]"`).
- **CI runs it too,** in `tests/integration/test_demo.py`.

A recording keeps a fingerprint of what the model was sent at every turn. If the platform
ever sends the model something else, the replay fails rather than showing a stale answer.
`python -m dip demo --record` records it again, live ($0.006 for these recordings, with `claude-haiku-5-5`).
The recording used here was made on 2026-10-23. Every value is invented.

```text
$ bash scripts/demo.sh

== 1. A vendor's eligibility file arrives without its member_id column
intake: quarantined ['SCHEMA.MISSING_COLUMN:member_id']

== 2. The intake opened an incident
incident 1: FILE.QUARANTINED (healthcare.eligibility/elig_20261021.csv: SCHEMA.MISSING_COLUMN:member_id)

== 3. The LangGraph chain: incident agent -> code -> triage agent
diagnosis: quarantined_records (0.85): The vendor file healthcare.eligibility/elig_20261021.csv was quarantined whole with SCHEMA.MISSING_COLUMN:member_id. Its header is first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref, which has no member_id column, while contract v1 requires member_id (pattern ^M[0-9]{9}$). The file instead carries patient_ref, which the contract describes as the payer's link to the clinical record. This is a vendor-side schema change, not a late or failed platform step.
handoff (decided in code): ['healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id']
proposal: approval 1 request_resend for healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id

== 4. The triage agent tries to run approval 1 before anyone approved it
agent: The server refused to run approval 1. It reports that the approval is still pending and that only a person's approval lets it run. I did not retry or try another id. Someone needs to confirm the approval was recorded before this can run.

== 5. A person approves approval 1; the agent runs it
approval 1: approved by human:demo@dip
agent: Approval 1 ran and returned status "executed." It sent the vendor a request_resend for quarantine entry 1 (elig_20261021.csv), which was rejected because the required column member_id is missing. The vendor's reply says they will resend under the same file name.

== 6. The vendor's corrected file arrives
intake: loaded, 1 quarantine entries superseded
incident 1: closed

== 7. The audit trail
  demo/incident          incident         get_incident                allowed
  demo/incident          incident         get_quarantine_sample       allowed
  demo/incident          incident         get_contract                allowed
  demo/incident          incident         get_feed_history            allowed
  demo/incident          incident         report_diagnosis            allowed
  demo/handoff           graph            handoff_to_triage           allowed
  demo/triage            triage           list_quarantine_groups      allowed
  demo/triage            triage           get_contract                allowed
  demo/triage            triage           get_feed_history            allowed
  demo/triage            triage           get_quarantine_sample       allowed
  demo/triage            triage           propose_operation           allowed
  demo/demo_act_denied   triage           execute_approved_operation  denied   approval 1 is pending: only a person's approval lets it run
  -                      human:demo@dip   approvals.approve           allowed
  demo/demo_act          triage           execute_approved_operation  allowed

demo complete
```

What each step uses:

| Step | Built in |
|---|---|
| 1–2. The breakage, the quarantine, the incident | `dip break` (Stage 4), the intake and contracts (Stages 1–2), incidents (Stage 7) |
| 3. Diagnosis, the handoff decided in code, the triage proposal | the incident agent and the LangGraph graph (Stage 7), the triage agent (Stage 4) |
| 4–5. Refused, then allowed after a person approved | the Act tier (Stage 6) |
| 6. The resend supersedes the quarantine and closes the incident | Stage 4 (S4.5) and Stage 7 |
| 7. The audit trail | the MCP server's registry (Stage 3 on) |

## Nightly evals

`.github/workflows/evals.yml` runs nightly at 04:17 UTC, and on demand. It replays every
agent's recorded evals against a Postgres service:
- the triage evals, including the held-out cases;
- Act mode;
- the break-and-fix loop;
- the incident chain;
- the contract author.

It uploads the reports and writes a summary table, and it fails if any case fails. The job
is never given a model key: no secret is mapped into it, and a guard step fails the run if
one is ever added to its environment. Live runs, for new recordings or variance, are a
person's, on a laptop with a key ([docs/agents/EVALS.md](../agents/EVALS.md)).

## The fresh-clone quickstart

Checked on 2026-10-23 on a 16 GB Windows laptop, Git Bash, from a fresh clone of `main`
(f05e351), following the README's quick start, with the observability profile:

```bash
git clone https://github.com/erjonb19/data-intake-platform && cd data-intake-platform
bash scripts/prereqs.sh
TF_VAR_profiles='["core", "observability"]' bash scripts/up.sh
bash scripts/smoke.sh
python -m venv .venv && .venv/Scripts/python -m pip install "boto3==1.43.*" \
  "psycopg[binary]==3.3.*" "pyyaml==6.*" "paramiko==3.5.*" "openpyxl==3.1.5" "jsonschema==4.*"
bash scripts/e2e.sh
bash scripts/down.sh
```

| Step | Result |
|---|---|
| `prereqs.sh` | PASS (Docker 29.4, kind 0.33, Terraform 1.16, kubectl 1.34; 8 GiB for Docker) |
| `up.sh` | PASS in 9 minutes. Docker already had the images' layers cached, so this is not a first-run time |
| `smoke.sh` | PASS |
| `e2e.sh` | PASS for every stage it reached: FHIR intake and the OMOP build; 22 file-feed cases across 6 feeds; the observability checks; Marquez lineage; the MCP Read tier |
| `down.sh` | PASS: the cluster and its local state removed |

The E2E's last stage, the break loop through the deployed stack (`tests/e2e/break_loop.py`),
did not finish. Claude Code, which ran the session, stopped the E2E process because the
laptop was critically low on memory: available memory had fallen from 3.2 GB to about
150 MB. That stage passes in CI's kind E2E on every push. The README now says to start with at
least 4 GB free.

## The screenshots

`python scripts/screenshots.py docs/images` drives a separate, headless Chrome over the
DevTools protocol, with its own throwaway profile. It logs in, opens each page and saves a
PNG. The README's "What it looks like" section shows the five from this run.

Two choices, both recorded here:
- **The quarantine frame is the intake task's log in Airflow**, which prints each file with
  its status and reason codes. MinIO's console lists the quarantined file and its
  `.reason.json`, but its preview did not open in the headless browser.
- **The approvals frame is terminal output**, because the approvals queue is the
  `dip approvals` CLI, not a web page. It shows the output as printed.

What the run found, and what changed:
- **Grafana was OOM-killed** (exit 137) at its 256 MiB limit while serving the Feeds
  dashboard to a browser. CI's checks use Grafana's API only, so they passed. The limit is
  now 384 MiB (`infra/helm-values/grafana.yaml`). For the screenshot, the running
  deployment was given the same limit with `kubectl set resources`.
- **The triage agent filed nothing in 2 of 3 live runs** over the stack's 15 quarantine
  groups. In the run that filed proposals, one turn used 7,853 output tokens against a cap
  of 8,000. The two runs that filed nothing almost certainly hit the cap mid-turn, and
  the loop treats any stop other than a tool call as the end.
  - The cap is now 16,000.
  - `dip triage run` and `act` warn, and exit non-zero, when the agent stops before it
    finishes.
  - The evals never had a queue this large or this mixed. Their largest case is the break
    loop's delimiter scenario: 10 groups, but all from one file with one cause.
- In the approvals frame, approval 1 came from the E2E's break-loop stage, filed before the
  E2E was stopped. Approvals 2–16 came from the triage agent's third live run. That frame
  shows proposals only; none was approved or run.
