# The MCP Read tier, deployed (Stage 3, S3.4)

The MCP server runs in the cluster (`infra/terraform/mcp.tf`, image `images/mcp`). It uses
Streamable HTTP at `/mcp` and is ClusterIP only. Any MCP client a person points at it
connects as the read-only `reader` agent:

```bash
kubectl -n dip-data port-forward svc/mcp 8765:8765
terraform -chdir=infra/terraform output -raw mcp_reader_key   # Authorization: Bearer <key>
# MCP client URL: http://127.0.0.1:8765/mcp
```

## Tools (policy `agents/policies/reader.yaml`)

| Tool | Reads |
|---|---|
| `list_quarantine_groups`, `get_quarantine_sample` | Open quarantine by feed and reason, with samples |
| `get_contract`, `get_feed_history` | The contract, and the feed's recent files |
| `get_dq_results` | Each dbt test's latest result |
| `get_dag_run`, `get_task_log` | Airflow runs, task states and log tails, as a Viewer login |
| `get_lineage` | What a table is built from, from Marquez, capped at 100 entries |
| `query_metrics` | A fixed set of Prometheus queries. The only parameter is a contract feed name, never free PromQL |
| `profile_file` | Rows, and per column the empty and distinct counts, of a received CSV or Excel file |

The server lists an agent only its own policy's tools, plus the Act-tier tool, which is
listed so that an attempt to use it shows in the audit log; it is always refused. A call
outside the policy is refused and audited too. `get_lineage` and `query_metrics` need the
observability profile. Without it, they return a clear "not configured" error.

## What the reader may see

- **PHI:** the reader policy sets `mask_phi: true`.
  - Samples and violation details hide the values of PHI columns, and of every column
    the contract does not declare, because a renamed column may be PHI under a new name.
  - A FHIR resource shows only its type, id and field names.
  - A file rejected whole shows only its header line.
  - `profile_file` shows top values only for declared, non-PHI columns.
  - The triage agent's policy does not mask, so it sees records as received.
- **Task logs:** these are the platform's own: codes, counts and file names. An error
  message can still quote a value, so logs are returned as untrusted content, and masking
  does not rewrite them.
- **Vendor content:** file contents, file names, log lines and landed-file names in
  lineage are all under `untrusted_vendor_content`, with a notice that they are data,
  never instructions.

## Least privilege

| Credential | May do |
|---|---|
| `dip_mcp` (Postgres) | Read `ops.quarantine`, `ops.intake_file` and `ops.dq_results`; insert into `ops.audit_log`. Nothing in bronze, silver, gold, the research tiers or approvals. It cannot update or delete the audit (`tests/integration/test_mcp_read.py`) |
| `mcp-read-*` (MinIO) | Get and list `raw/` and `quarantine/` only |
| `mcp-viewer` (Airflow) | The Viewer role |
| Bearer key | One key for `reader`. Only its SHA-256 is in the server's Secret |

## The demo question

The question is "why did the eligibility feed quarantine rows?". `tests/e2e/mcp_read.py`
answers it from MCP alone, in this order:

1. the open groups: a `plan_code` value the contract does not allow, and a file missing
   `member_id`
2. a sample, showing the rejected value `GOLD` with every PHI value hidden
3. the contract's allowed codes
4. the feed history
5. the DAG run and its intake log
6. the file profile, which finds the missing column
7. the metric and the lineage behind it

The same check makes sure a bad or missing key gets 401, a propose call is refused, and
every call is audited.
