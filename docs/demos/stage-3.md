# Stage 3 demo: OCR, monitoring, lineage and the read-only MCP tier

What Stage 3 adds (PLAN.md section 8.7), and the runs that prove it.

## The local proof (2026-10-23)

One run on a 16 GB Windows laptop, Git Bash, the `core` profile (the observability
profile was left to CI to save memory), from a clean kind cluster:

```bash
bash scripts/up.sh && bash scripts/smoke.sh && bash scripts/e2e.sh
bash scripts/down.sh
```

| | Result |
|---|---|
| Wall time, up + smoke + E2E | 34 min 24 s (up.sh builds four images on a first run) |
| kind node memory at the end | 3.29 GiB |
| Smoke | PASS |
| FHIR intake and OMOP build | PASS |
| File-feed cases | 17 across 4 feeds, PASS (eligibility, claims, Excel invoices, PDF invoices native and OCR'd; MinIO, SFTP and API channels) |
| MCP Read tier | Every check but one passed: 401 without a key or with a wrong one, the reader's tool listing, PHI masking, `profile_file` as the read-only MinIO user, DQ results, the DAG run, policy refusal of a propose call, every call audited. The task-log check failed; CI showed why (the newest run of the feed had found landing empty) and the check now looks across recent runs |

## In CI

The `e2e` workflow deploys the stack with the observability profile on every push and
runs, after the file-feed cases:

- `tests/e2e/observability.py`: Prometheus scrapes its 3 targets, every warehouse metrics
  query runs as `dip_metrics`, the 7 alert rules are loaded, Grafana is up, refuses an
  anonymous call and has both dashboards;
- `tests/e2e/lineage.py`: in Marquez, the feed's intake job is upstream of
  `gold.member_eligibility` and `gold.invoice_match`, a run of that job read the landed
  file, and the gold table carries its dbt test results;
- `tests/e2e/mcp_read.py`: the Read tier answers "why did the eligibility feed quarantine
  rows?" from MCP alone (docs/agents/mcp-read.md).

The `upgrade` workflow deploys the previous `main`, loads data, upgrades in place, and runs
the E2E again; it found the R7 classification bug fixed in #38.

## Try it

```bash
TF_VAR_profiles='["core", "observability"]' bash scripts/up.sh   # about 0.8 GB more
bash scripts/e2e.sh
kubectl -n dip-obs port-forward svc/grafana 3000:80             # Feeds and DAG runs dashboards
kubectl -n dip-data port-forward svc/mcp 8765:8765              # the MCP Read tier
terraform -chdir=infra/terraform output -raw mcp_reader_key
```
