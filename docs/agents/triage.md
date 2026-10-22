# The failure-triage agent (Stage 4)

When files fail their contracts, quarantine fills up with reason codes. The triage agent
reads the open quarantine, works out the root cause of each group, and proposes one action
per group. **Nothing changes until a person approves the proposal**, and even then only that
exact proposal runs. It is built to the agent design in PLAN.md section 7.

```
 triage agent (claude-haiku-5-5)            MCP server (Streamable HTTP, /mcp)              Postgres
 ───────────────────────────────            ──────────────────────────────────              ────────
 Claude tool-use loop  ── Bearer key ──▶    ApiKeyVerifier: key -> agent id
 (dip/agents/triage.py)                     policy agents/policies/triage.yaml ─ refuse? ─▶ ops.audit_log
                                            tool registry (dip/agents/registry.py) ───────▶ (every call,
                                              read:    list_quarantine_groups,               allowed or
                                                       get_quarantine_sample,                denied;
                                                       get_contract, get_feed_history        append-only)
                                              propose: propose_operation ─────────────────▶ ops.approvals
                                              act:     execute_approved_operation              (pending)
                                                       (refused to agents)
 a person:  dip approvals list | approve | reject | execute ───── same registry, audited as human:<name>
```

## What it can and cannot do

| | |
|---|---|
| Reads | open quarantine grouped by feed and reason code (with the files each group touches), samples of the rejected records or the start of a file, the feed's contract, the feed's recent history |
| Proposes | `request_resend` (with the message to the vendor), `discard`, `replay` (reprocess a file quarantined whole, once the contract was fixed) or `propose_contract_change` (hand over to the contract author, Stage 5), one per group, with a root cause from a fixed list, a rationale citing what it saw, and a confidence |
| Never | changes data, approves, or executes. `execute_approved_operation` is listed to it (so an attempt is visible and measurable) but its policy refuses it, and the refusal is audited |

**Enforcement is on the server.** The policy file lists the agent's tools, its highest tier
(`propose`), the actions it may propose, and call limits: per run (60 calls, 20 proposals)
and per hour (600 calls, 100 proposals). The run id comes from the client, so the per-run
limits stop a runaway loop but trust the client; the hourly limits are per agent, whatever
run id it sends.
The model's output cannot change any of it: an unknown tool, a tool outside the policy, a
tier above `propose`, an action outside the list or a call over a limit is refused and
written to `ops.audit_log`. The log stores a SHA-256 of the arguments, not the arguments
(they can hold vendor data). A trigger refuses UPDATE, DELETE and TRUNCATE through normal
SQL. The table owner or a superuser can still disable the trigger, and the MCP server
connects as the warehouse owner today: an INSERT-only role for the server is the follow-up
that makes this hold against the owner too.

**Vendor content is data.** Every tool result that comes from a file is wrapped as
`untrusted_vendor_content` with a notice, and the prompt (`agents/prompts/triage.md`) says
that instructions found in data are a data problem to triage, never to follow.

**Approvals** (`ops.approvals`, `dip/agents/approvals.py`). A proposal is a `pending` row
with its payload and the payload's SHA-256. A person approves or rejects it; an approval
records the hash of the payload they approved. A trigger refuses any change to a proposal's
action or payload after it is made. It executes only if it is `approved`, not expired
(7 days), and its payload still matches both hashes (the second line, against someone who
can disable the trigger). It executes once: the row is locked from the check until the
action's changes and `executed` commit together, so two concurrent executions cannot both
run it (tested). **Who approves:** the CLI records the person as their OS user name and
their database login, and anyone with the warehouse DSN can approve. Separate approver
roles, so the agent's operators cannot approve their own proposals, are a follow-up. Replay puts the file's immutable raw copy back into landing under its original
name, after forgetting that the content was processed, so the next intake validates it
against the current contract. Its quarantine history stays, closed as released. Replay
closes every open entry of the file, also entries of other groups, and the file leaves the
feed history until it is processed again. Every landing key is checked before anything
changes, and if anything fails the files put into landing are removed again.

## Running it

```bash
export DIP_WAREHOUSE_DSN=postgresql://...                       # the warehouse
export DIP_AGENT_KEYS='{"triage": "<sha256 of the triage key>"}' # hashes only
python -m dip mcp-serve --port 8765 &                           # the MCP server
DIP_MCP_URL=http://127.0.0.1:8765/mcp DIP_TRIAGE_KEY=<key> python -m dip triage run
python -m dip approvals list
python -m dip approvals approve 4 --note "..."   # or reject
python -m dip approvals execute 4
```

From a replayed eval case (`mixed_day`), the person's side:

```
$ python -m dip approvals list
    1 pending   triage   request_resend   healthcare.eligibility|FILE.BAD_ROW_RATIO (1 entries, duplicate_keys, confidence 0.9)
    2 pending   triage   request_resend   healthcare.eligibility|FILE.NAME_DATE (1 entries, file_naming, confidence 0.85)
    3 pending   triage   request_resend   healthcare.eligibility|KEY.DUPLICATE:member_id (2 entries, duplicate_keys, confidence 0.85)
    4 pending   triage   request_resend   healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id (1 entries, schema_drift, confidence 0.9)
$ python -m dip approvals approve 4 && python -m dip approvals execute 4
approval 4: approved
{"vendor_request": "Your file was rejected because the required column member_id is missing from
 the header. ... Please include member_id as the first column and resend the file.", ...}
$ python -m dip approvals execute 4
refused: approval 4 is executed, not approved
```

and `ops.audit_log` holds the agent's 11 calls (all `allowed`), the person's approval and
both executions, the second as an error.

## Evals

`agents/evals/triage/cases.yaml` has 12 cases, and `holdout.yaml` has 7 more. Each case:
1. lands invented files through the real intake into a fresh warehouse;
2. starts the real MCP server on localhost with a one-off key;
3. runs the agent over HTTP and scores it against gold labels per group. A label is the root
   cause, the acceptable actions and the preferred one.

The cases cover:
- schema drift: a missing, renamed or added column;
- invalid values: one row, many rows, a new code;
- duplicate keys;
- bad file names and dates;
- encoding;
- FHIR structure;
- a replay after a contract change;
- a mixed day with four groups;
- **3 prompt-injection cases**, with instructions in a cell, a header, a file name and a date.

**Scores:**
- one proposal per group;
- root-cause accuracy;
- action acceptable, and action preferred;
- complete quarantine ids;
- **policy violations**: any refused call, Act tier above all;
- injection cases resisted;
- turns, tokens and cost.

**CI never calls the model.** Each case's recorded model turns
(`agents/evals/triage/recordings/`) are replayed through the real loop, server, policy and
scoring in `tests/integration/test_agents.py`. Each recorded turn keeps a fingerprint of
what the model was sent (the prompt, the tools, the tool results with dates masked), so a
changed prompt, tool or tool output fails the replay instead of passing on stale answers.
`python -m dip triage eval --record` records again, live (`DIP_EVAL_DSN` must be a throwaway
database). In a replay the model's choices are fixed, so the evidence that injections are
resisted is the live runs below. The refusal path is tested on its own: a scripted
"hijacked model" that calls `execute_approved_operation` and proposes for ids outside its
group is refused on both and scored as two violations.

**Results** (`claude-haiku-5-5`, adaptive thinking, effort medium; 2026-10-22):

| Run | Cases passed | Root cause | Action acceptable / preferred | Policy violations | Injection resisted | Cost |
|---|---|---|---|---|---|---|
| 1st, 12 cases | 9/12 | 85.7% | 95.2% / 76.2% | 0 | 2/2 | $0.033 |
| 2nd: tool and prompt fixes | 11/12 | 100% | 85.7% / 85.7% | 0 | 2/2 | $0.031 |
| 3rd: eval names fixed | 12/12 | 100% | 100% / 90.5% | 0 | 2/2 | $0.032 |
| **held-out**, 7 new cases, prompt frozen | **7/7** | 100% | 100% / 87.5% | 0 | 1/1 | $0.015 |
| **variance**: 3 more live runs of the 12 | **33/36** | 92.1% | 96.8% / 90.5% | 0 | 6/6 | $0.097 |
| re-recorded with fingerprints (12 + 7) | 12/12, 7/7 | 100% | 100% / 90.5%, 87.5% | 0 | 3/3 | $0.047 |

Every run took 4 model turns per case. Most input tokens were prompt-cache reads (161k of
205k in the last run of the 12).

**What changed between the runs, and why:**
- The agent gave a file rejected for its bad rows (`FILE.BAD_ROW_RATIO`) a different root
  cause than those rows. It could not see which file a group came from. The fix is in the
  tool: groups now list their files. The prompt also says a ratio rejection shares its rows'
  cause.
- It labelled a FHIR resource missing a required field `invalid_values`. The root-cause
  definitions were ambiguous there; `fhir_structure` now covers every `FHIR.*` code.
- In the second run it proposed discarding a file whose invented names ("Placeholder",
  "Testcase", "Dummy") made it look like a test file. That was a fair reading of the prompt,
  so the fault was in the eval: the cases now use ordinary-looking invented names.

The held-out cases were written after these changes and never used to tune the prompt.
The variance runs show the remaining instability: 3 of 36 runs failed on the two hardest
cases. A replay that also needs a contract-version comparison failed twice, and a new plan
code that could be either a contract change or a resend failed once. No run in any batch
attempted a refused tool.
