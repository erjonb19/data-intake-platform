# Keys and their rotation (24.30)

The MCP server knows each agent by an API key. Each key is stored there only as its SHA-256.
The scheduled runners hold the keys in their own Secrets: `dip-agent-chain` (incident,
triage) and `dip-intake-team` (profile, contract author, validator, reviewer,
orchestrator). The reader key is for people and tests. The run token key signs every chain a
runner starts (24.10). Terraform generates all of them (`infra/terraform/mcp.tf`).

## When to rotate

- **Every month**, on the first working day: set the generation to the month (`2026-12`).
- **At once** if a key may have leaked: a key pasted into a chat, an issue, a log or a
  screenshot, or a laptop lost with the repo's Terraform state on it. Do it before anything
  else. Then look for calls under that agent you can't explain: on the approvals page's Audit
  log tab, filter by its principal.
- **Before giving anyone else access** to the cluster or its state.

## How

One value replaces them all in a single apply, so no client keeps an old key. Run it in the
same PowerShell window you deploy from, with the same `TF_VAR_*` values as last time (the
profiles) and the same model key in `ANTHROPIC_API_KEY` (24.39). A fresh window without them
would also switch those off:

```powershell
$env:TF_VAR_mcp_key_generation = "2026-12"
& "$env:ProgramFiles\Git\bin\bash.exe" scripts/up.sh
```

Keep that value on later runs of `up.sh` (set it in your PowerShell profile, or pass it each
time). A run without it goes back to the default, `1`, and that is a change too: it rotates
the keys again.

What happens in that apply:
- Terraform replaces the 8 MCP keys and the run token key.
- It writes the new hashes into the MCP server's Secret. The Deployment rolls, because its
  `dip/config-sha` annotation changes.
- It writes the new keys into the runners' Secrets. The next task pod reads them at start.
- A chain that is running at that moment gets 401 on its next MCP call and fails. Run it
  again after the apply.

To check it:

```powershell
$env:KUBECONFIG = terraform -chdir=infra/terraform output -raw kubeconfig_path
kubectl -n dip-data rollout status deploy/mcp
```

The next `agent_chain` run's audit rows show its calls allowed. During the rollout, the old pod
still answers for a few seconds and refuses the new keys (401).

The first apply after 24.30 merged rotated every key once by itself, because the generation
was new.

## What this does not rotate

| Credential | How it changes |
|---|---|
| Intake API client keys (`infra/terraform/intake_api.tf`) | A vendor holds them, so they change on request, with the vendor: `-replace='random_password.api_key["<client>"]'` |
| Approvals page tokens (`approvals.tf`) | Per person: `-replace='random_password.approver_token["<name>"]'`, then paste the new `approvals_token` |
| Database and MinIO passwords (`secrets.tf`) | `-replace` the one `random_password.this["<name>"]`. The roles Job and the MinIO user Jobs set them again on apply |
| Model API keys | At the provider's console. Then pass the new one as before |

## Before anything is hosted

Static keys are fine on a laptop cluster that only this machine can reach. A hosted
deployment should give agents short-lived tokens with an audience (`aud`: the MCP server's
URL) and an expiry of minutes, issued per run by an identity provider (20.1, Keycloak). The
MCP server would check the signature, the audience and the expiry instead of a hash. The run
token (24.10) already works this way for chains: HMAC-signed, bound to one chain, with an
expiry of 2 hours.
