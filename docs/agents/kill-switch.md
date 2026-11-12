# The agents' kill switch (24.8)

One switch stops every agent from changing anything, without stopping the platform.

**What it does.** With `DIP_AGENTS_WRITE_DISABLED` set to `1`, `true` or `yes`:

- The MCP server refuses every **propose** and **act** tool call made by an agent. (Read-tier
  tools that record a check, like `validate_contract`'s candidate row, still run: they change
  no data and nothing is proposed without a propose-tier call.) Each
  refusal is a `denied` row in `ops.audit_log`, with the reason "agent write tools are
  switched off", like any other refusal. Read tools still answer, so a person can see what
  the agents saw.
- The scheduled runners (`agent_chain`'s `run_chain` and `intake_jobs`) claim nothing, so
  open incidents and onboarding jobs wait instead of failing.
- People are not affected. Approving, rejecting and executing through `python -m dip
  approvals` or the approvals page still work.

**Turning it on.**

```bash
# The lasting way: both the MCP server and the agent runners get it.
terraform -chdir=infra/terraform apply -var agents_write_disabled=true
# The MCP Deployment's config-sha annotation changes with its Secret, so the pod rolls.

# The fast way, in an emergency (until the next terraform apply):
kubectl -n dip-data set env deploy/mcp DIP_AGENTS_WRITE_DISABLED=1
```

`kubectl set env` restarts the MCP pod with the switch on at once. The runners follow the
`dip-agent-chain` Secret, so to stop them too, apply with Terraform (or pause the
`agent_chain` DAG in Airflow).

**Turning it off.** `terraform apply` without the variable (it defaults to `false`), or
`kubectl -n dip-data set env deploy/mcp DIP_AGENTS_WRITE_DISABLED-` after the fast way.

**Checking it.**
`select at, principal, tool, decision, reason from ops.audit_log order by at desc limit 10;`
shows the denied calls. While it is on, each `agent_chain` run logs "agent write tools are
switched off".
