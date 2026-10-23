# Stage 7 demo: an incident, diagnosed and handed to triage

What Stage 7 adds (PLAN.md section 8.10, S7.1–S7.4): a breakage opens an incident; the
incident agent diagnoses it; the LangGraph graph's code decides whether to hand it to the
triage agent, through a typed, audited edge; triage proposes for a person to approve. How
it works, and the evals: [docs/agents/incident.md](../agents/incident.md).

## The run (2026-10-23)

This was run for real on a laptop, against a throwaway Postgres, with the MCP server running
locally (`python -m dip mcp-serve`, both agents' keys in `DIP_AGENT_KEYS`) and
`claude-haiku-5-5`. The file is invented; the output is as printed.

```text
$ python -m dip land healthcare.eligibility elig_20261023.csv && python -m dip intake
landed 1 file(s) for healthcare.eligibility
1 file(s): quarantined=1
quarantined ['FILE.BAD_ROW_RATIO', 'VALUE.NOT_ALLOWED:plan_code']
$ psql -c "select incident_id, feed, kind, detail from ops.incident"
 incident_id |          feed          |       kind       |                                          detail
-------------+------------------------+------------------+-------------------------------------------------------------------------------------------
           1 | healthcare.eligibility | FILE.QUARANTINED | healthcare.eligibility/elig_20261023.csv: FILE.BAD_ROW_RATIO, VALUE.NOT_ALLOWED:plan_code
(1 row)

$ python -m dip incident run --incident 1
$0.0068
chain chain-0d38d8d4dd: incident 1 -> quarantined_records (0.9): The vendor file elig_20261023.csv was quarantined whole. Its 3 rows included 2 with plan_code values outside the contract's allowed list (HMO, PPO, EPO, HDHP). That is 2 of 3 rows, above the contract's max_bad_row_ratio of 0.05, so the file was rejected as FILE.BAD_ROW_RATIO. The vendor sent bad data; this is not a platform or transform problem.
handoff to triage: ['healthcare.eligibility|FILE.BAD_ROW_RATIO', 'healthcare.eligibility|VALUE.NOT_ALLOWED:plan_code'] (3 entries)
approval 1: request_resend           healthcare.eligibility|VALUE.NOT_ALLOWED:plan_code
approval 2: request_resend           healthcare.eligibility|FILE.BAD_ROW_RATIO
$ psql -c "select run_id, principal, tool, decision from ops.audit_log order by audit_id"
          run_id           | principal |          tool          | decision
---------------------------+-----------+------------------------+----------
 chain-0d38d8d4dd/incident | incident  | get_incident           | allowed
 chain-0d38d8d4dd/incident | incident  | get_contract           | allowed
 chain-0d38d8d4dd/incident | incident  | get_quarantine_sample  | allowed
 chain-0d38d8d4dd/incident | incident  | get_quarantine_sample  | allowed
 chain-0d38d8d4dd/incident | incident  | get_feed_history       | allowed
 chain-0d38d8d4dd/incident | incident  | report_diagnosis       | allowed
 chain-0d38d8d4dd/incident | incident  | report_diagnosis       | allowed
 chain-0d38d8d4dd/handoff  | graph     | handoff_to_triage      | allowed
 chain-0d38d8d4dd/triage   | triage    | list_quarantine_groups | allowed
 chain-0d38d8d4dd/triage   | triage    | get_quarantine_sample  | allowed
 chain-0d38d8d4dd/triage   | triage    | get_quarantine_sample  | allowed
 chain-0d38d8d4dd/triage   | triage    | get_contract           | allowed
 chain-0d38d8d4dd/triage   | triage    | get_feed_history       | allowed
 chain-0d38d8d4dd/triage   | triage    | propose_operation      | allowed
 chain-0d38d8d4dd/triage   | triage    | propose_operation      | allowed
(15 rows)

 approval_id | agent_id |     action     | status  |                        grp
-------------+----------+----------------+---------+----------------------------------------------------
           1 | triage   | request_resend | pending | healthcare.eligibility|VALUE.NOT_ALLOWED:plan_code
           2 | triage   | request_resend | pending | healthcare.eligibility|FILE.BAD_ROW_RATIO
(2 rows)
```

What happened:
- **The breakage opened the incident.** The intake quarantined the file whole and opened
  a `FILE.QUARANTINED` incident for the feed.
- **The agent diagnosed it.** It read the incident, the contract, two samples and the feed
  history, then reported `quarantined_records` with confidence 0.9. It reported twice; the
  policy allows two per run, and the graph uses the latest.
- **Code decided the handoff.** The graph built `HandoffToTriage` with the incident's own
  open quarantine (3 entries, 2 groups) and audited it as `graph / handoff_to_triage`.
- **Triage proposed.** It proposed a resend for each group. Both approvals wait for a
  person (Stage 6 covers what happens after approval).
- **One chain id ties it together.** The whole chain is in `ops.audit_log` under
  `chain-0d38d8d4dd/…`.

## Evals

6 recorded cases pass (CI replays them), and 18 of 18 across 3 live runs. Two cases are
injection attempts: one in a vendor's file name and one in a task log. The table is in
[docs/agents/incident.md](../agents/incident.md#evals-s74).
