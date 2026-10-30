# Break-and-fix runbooks (Stage 4, PLAN.md section 8)

Eight failures a vendor feed really has, each with what you see, where the evidence is,
what to do, and how to check the fix. Every runbook names the platform's real reason codes
(docs/contracts.md), alerts (`observability/prometheus/rules.yml`), Read-tier MCP tools
(docs/agents/mcp-read.md) and commands. Seven of the eight (1–5, 7 and 8) can be triggered on
purpose with `python -m dip break` (dip/breakages.py) on a good file, and so can a broken
X12 envelope (`x12_segment_lost`, `X12.ENVELOPE:SE01`: handled like runbook 4).

| # | Runbook | Reason code or signal | Break it with |
|---|---|---|---|
| 1 | [A column renamed or dropped](01-missing-column.md) | `SCHEMA.MISSING_COLUMN:<col>` | `rename_column`, `drop_column` (and `reorder_columns`, which must load as it is) |
| 2 | [A new column](02-unexpected-column.md) | `SCHEMA.UNEXPECTED_COLUMN:<col>` | `add_column` |
| 3 | [A new or junk code value](03-code-values.md) | `VALUE.NOT_ALLOWED:<col>`, `VALUE.TYPE:<col>` | `new_code`, `junk_values`, `type_change` |
| 4 | [Delimiter or encoding changed](04-delimiter-encoding.md) | `SCHEMA.MISSING_COLUMN`, `FILE.ENCODING` | `delimiter` |
| 5 | [A late file](05-late-file.md) | `FEED.LATE` incident, alert FeedLate | `late_file` (hold the file back) |
| 6 | [Duplicate keys](06-duplicate-keys.md) | `KEY.DUPLICATE:<col>` | (the finance fixture `invoices_breakages.xlsx`) |
| 7 | [An unreadable scan](07-low-confidence-scan.md) | `OCR.LOW_CONFIDENCE`, `PDF.INVALID` | `truncated_pdf` (and the fixture `invoice_scan_poor.pdf`) |
| 8 | [Instructions hidden in a vendor file](08-prompt-injection.md) | a denied tool call in `ops.audit_log` | `inject_cell` |

## How every runbook works

1. **Symptom.** A reason code in `ops.quarantine`, an alert in Prometheus (`/alerts`), or a
   Grafana panel (Feeds dashboard).
2. **Evidence**, all read-only: the MCP Read tier as the `reader` agent
   (`list_quarantine_groups`, `get_quarantine_sample`, `profile_file`, `get_feed_history`,
   `get_contract`, `query_metrics`, `get_lineage`, `get_dag_run`, `get_task_log`), or the
   same facts in SQL and the UIs. PHI is masked for the reader.
3. **Fix**, always proposed and approved, never done by an agent:
   - the triage agent proposes one action per group (`python -m dip triage run`), or a
     person decides directly;
   - a person reviews with `python -m dip approvals list|show <id>` and decides with
     `approve <id>` or `reject <id>`, then runs `execute <id>`;
   - a contract change goes through the contract author (`propose_contract_change` hands
     it over) and a local branch the person publishes with `gh pr create`
     (docs/agents/contract-author.md).
4. **Check.** The file's status in `ops.intake_file`, the group gone from
   `list_quarantine_groups`, and the metric back to normal.

The E2E (`tests/e2e/cases.yaml`) covers the reason codes of runbooks 1, 3, 6 and 7 with
committed fixtures, and the contract-author evals cover the scenarios of runbooks 1–4 and
8. **The whole loop** (break, detect, triage, propose, approve, fix, check) runs for every
`dip break` scenario, which covers runbooks 1–4 and 8 and the X12 envelope:
`python -m dip triage loop`, replayed in CI by `tests/integration/test_break_loop.py`,
and once through the deployed stack in the kind E2E (`tests/e2e/break_loop.py`).
Results: [docs/agents/triage.md](../agents/triage.md#the-break-and-fix-loop-stage-4).
