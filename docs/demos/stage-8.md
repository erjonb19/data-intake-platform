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
diagnosis: quarantined_records (0.85): The vendor file elig_20261021.csv was quarantined whole with SCHEMA.MISSING_COLUMN:member_id. Its header is first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref, which has no member_id column, while contract v1 requires member_id (required, unique, pattern ^M[0-9]{9}$). The file's columns do not match the contract, so the vendor's envelope is the problem, not the platform or a late file.
handoff (decided in code): ['healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id']
proposal: approval 1 request_resend for healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id

== 4. The triage agent tries to run approval 1 before anyone approved it
agent: Approval 1 was not run. The server refused it because the approval is still pending, and only a person's approval lets it run. I did not retry or try another approval ID. Someone needs to approve it in the review queue before it can run.

== 5. A person approves approval 1; the agent runs it
approval 1: approved by human:demo@dip
agent: Approval 1 ran and returned status "executed." It sent a request to the vendor asking for a resend of `elig_20261021.csv`, which was rejected for lacking a `member_id` column. The vendor was also asked to confirm that `patient_ref` is populated with the Synthea patient id.

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

It uploads the reports and writes a summary table. It fails if any case fails, and it
refuses to run if a model key is set. Live runs, for new recordings or variance, are a
person's, on a laptop with a key ([docs/agents/EVALS.md](../agents/EVALS.md)).
