# Security audit, 2026-11-10 (backlog 24.1)

Plan only: no code was changed. The repo (`main` at 233b21d, after #116 and #122) was
audited against `docs/SECURITY_STANDARDS.md`:

- Section 19.3, the new-repo checklist.
- Every section that applies to this stack: 3, 4, 5–9, 10–13, 14–15, 16.3–16.5, 17 and 18.

Four read-only reviews covered the sections in parallel. I checked the key claims myself
against the code, and merged findings that more than one review reported. No `.env`,
credential or state file was opened.

**Scope and exposure.** Everything runs on a local kind cluster. Every NodePort binds to
127.0.0.1, every service is ClusterIP, and the data is synthetic or public and
de-identified. Severities are rated by that real exposure. A finding that only matters
once something is hosted says so.

**Result.** No Critical finding (CVSS 9+). 3 High, 14 Medium, 24 Low. Each one has a
backlog item, 24.2 to 24.42 (the last section). No Critical means the Order of work does
not change; where the items go is the owner's decision when he merges this.

## Section 19.3, new-repo setup

| Item | State |
|---|---|
| `docs/SECURITY_STANDARDS.md`; Prime Directives in `CLAUDE.md` | Done (#122) |
| `.claude/settings.json` from 17.2 | Not added; proposed below (24.42) |
| `.gitignore` covers secrets and state; `.env.example` committed | Mostly: `.env*`, `*.pem`, `*.key`, `*.tfstate*`, `.terraform/`, kubeconfig and `.kube/` are ignored and `.env.example` names only three variables. Missing: `*.p12`, `secrets/`, credential JSON (24.26) |
| Cooldown and script settings (Section 4) | Missing: no lockfile, no hashes, no release-age cooldown (24.4) |
| `pre-commit` with gitleaks | Missing: the tracked hook checks data files only (24.20) |
| CI security job, actions pinned by SHA, read-only default permissions | Partly: read-only defaults everywhere, Trivy and CodeQL run (#116), but actions are pinned by tag (24.5) |
| Branch protection on `main` | Off: `branches/main/protection` returns 404; no rulesets (24.6, the owner's step) |
| Secret scanning, push protection, Dependabot alerts | Secret scanning and push protection on; Dependabot alerts and security updates off (24.7) |
| `SECURITY.md` and how to report | Missing, and private vulnerability reporting is off (24.24). Per CLAUDE.md, no personal email: point to GitHub's private reporting |

## Findings

### High

```
[SEVERITY: High] Every Airflow pod holds the platform's master secrets
Where: infra/terraform/airflow.tf:247 (AIRFLOW_CONN_MINIO = MinIO root); infra/helm-values/airflow.yaml:26-49 (chart `secret:` list)
Risk: The API server, scheduler and every task pod get MinIO root, the warehouse owner login, DIP_DEID_KEY and the SFTP private key. Task pods parse untrusted vendor PDF, Excel and X12 files, so one parser exploit means read and write on every bucket (backups included), the warehouse, and the de-identification key.
Fix: Give Airflow its own bucket-scoped MinIO user (intake API, MCP and backup already have one). Mount DIP_DEID_KEY and the SFTP key only into the pod_override of the DAGs that need them, not the chart's global secret list.
Standard: §16.4 (MinIO, Kubernetes), §10.2; OWASP A01:2025, A02:2025; CWE-250, CWE-522
```

```
[SEVERITY: High] The scheduled agent chain makes live model calls with no spend cap
Where: dags/agent_chain.py:78 (the subprocess env); infra/terraform/airflow.tf:22-33 (dip-agent-chain Secret); dip/agents/llm.py:116-118
Risk: Once a model key is set in the dip-agent-chain Secret, the every-5-minutes DAG calls the model with no DIP_SPEND_CAP_USD: check_spend returns early without it, and its count is per process. The $5/month rule would rest on a $0.50/day alert alone, against CLAUDE.md's "every live call runs under DIP_SPEND_CAP_USD".
Fix: Set DIP_SPEND_CAP_USD in the Secret. Add a pre-call check that sums the month's ops.model_call.cost_usd and refuses at $5. Set a provider-side limit in the Anthropic Console. Refuse an unknown DIP_LLM_MODEL instead of pricing it at Haiku's rate.
Standard: §14 (LLM10: unbounded consumption), §15.2 budgets; OWASP Agentic ASI08
```

```
[SEVERITY: High] Python installs have no lockfile, no hashes and no release-age cooldown
Where: pyproject.toml:10-51 (`>=` ranges and `==3.3.*` wildcards); .github/workflows/ci.yml:83, evals.yml:45, proof.yml:84,185,272,367,539, e2e.yml:64; images/airflow/Dockerfile:13 (agent venv), images/*/Dockerfile pip lines
Risk: Transitive dependencies resolve at install time. A malicious release published hours earlier would land in the proof jobs, which hold the paid model keys, in the agent venv that holds every agent key, and in the images 12.10 would publish.
Fix: A committed lock (`uv.lock` with `uv sync --locked`, or `uv pip compile --generate-hashes` requirements files installed with `pip install --require-hashes`), and a cooldown: uv `[tool.uv] exclude-newer = "7 days"` (https://docs.astral.sh/uv/reference/settings/#exclude-newer), or pip `--uploaded-prior-to P7D` (https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-uploaded-prior-to). Moving to uv is a new tool: the owner's OK (directive 11).
Standard: §4.3, §4.6; OWASP A03:2025, A08:2025; LLM03; CWE-829, CWE-494
```

### Medium

```
[SEVERITY: Medium] GitHub Actions are pinned by tag, not by commit SHA
Where: every `uses:` in the 8 workflows (e.g. .github/workflows/ci.yml:15, release.yml:34, proof.yml:53)
Risk: A moved tag runs new code in jobs that hold the model keys (proof.yml), `contents: write` (commit-results) or `packages: write` (release.yml). All are GitHub- or HashiCorp-owned, which lowers the likelihood.
Fix: Pin each to a full SHA with a `# vN.M` comment; the github-actions Dependabot ecosystem keeps them current.
Standard: §16.5, §4.4; OWASP A03:2025; CWE-829
```

```
[SEVERITY: Medium] `main` is not protected
Where: repo settings (branches/main/protection: 404; rulesets: [])
Risk: A direct or force push to main skips CI, the replay suite and review; "green before merge" has no technical guard.
Fix: (owner) A ruleset on main: PRs required; `lint`, `test`, `e2e` required; force push and deletion blocked. Steps are in #116.
Standard: §16.5, §19.3; OWASP A08:2025
```

```
[SEVERITY: Medium] Dependabot alerts and security updates are off
Where: repo settings (vulnerability-alerts: 404; dependabot_security_updates: disabled)
Risk: A new CVE in a pinned dependency raises nothing; the weekly version PRs (12.9) are not alerts.
Fix: (owner) Turn on Dependabot alerts and security updates (Settings, Advanced Security).
Standard: §4.6, §19.3; OWASP A03:2025
```

```
[SEVERITY: Medium] No kill switch for the agents' write tools
Where: dip/agents/registry.py:216 (policy check); dip/agents/mcp_server.py:113
Risk: Stopping every agent Propose or Act call at once means scaling pods down or rotating keys; a looping agent keeps filing proposals until a per-hour limit trips.
Fix: DIP_AGENTS_WRITE_DISABLED, checked in Registry.check: refuse every propose and act call for a non-human principal, audit it as denied; the chain DAG skips while it is set; a test.
Standard: §15.2 kill switch; OWASP Agentic ASI10
```

```
[SEVERITY: Medium] The agent-chain pod holds every agent key and the warehouse owner login
Where: infra/terraform/airflow.tf:22-35; dags/agent_chain.py:78,122
Risk: One pod's environment has seven agent keys, both model keys and the `warehouse` owner DSN (which can disable the approvals trigger). Compromising it undoes per-agent least privilege.
Fix: A dedicated login with only the ops.model_call and claim grants the chain needs; mount each step's key into its own pod, or at least split incident/triage from the intake team.
Standard: §15.1 ASI03, §5.3; LLM06
```

```
[SEVERITY: Medium] The agent chooses the run and chain id that scope its access
Where: dip/agents/mcp_server.py:118 (`args.pop("_run_id")`); dip/agents/registry.py:225
Risk: A prompt-injected contract_author can send another chain's id and read that chain's intake profile (which the "another chain" check is meant to hide), write audit rows under another chain, and dodge max_per_run by changing ids. Hourly caps still apply.
Fix: The server issues the run id (an HMAC-signed token minted when the job is claimed, checked in on_call_tool), or binds run-id prefixes to the job row the agent was assigned; a test that a key cannot read a chain it was not given.
Standard: §6.1 BOLA, §6.2; OWASP A01:2025, API1:2023; CWE-639
```

```
[SEVERITY: Medium] Approvals: one shared token, and the reviewer names himself
Where: dip/agents/gateway.py:67-74, 175-182, 343-353
Risk: Whoever holds DIP_APPROVALS_TOKEN (a Terraform output) can approve as any name; the X-Reviewer header becomes `decided_by`, so the audit cannot prove who decided.
Fix: One token per person, stored as a SHA-256 hash in a map (like DIP_AGENT_KEYS); the reviewer name comes from the matched token, not a header; revoke by removing the entry.
Standard: §5.3, §6.2, §15.2 audit; OWASP A07:2025; CWE-290
```

```
[SEVERITY: Medium] Backups are unencrypted and contain the de-identification key
Where: infra/terraform/backups.tf:210; docs/backups.md:139
Risk: The pg_dump includes ops_secret.deid_key in plaintext. Anyone with MinIO root (every Airflow pod, the first High) can read it and re-link pseudonyms in the research tier.
Fix: `--exclude-schema=ops_secret` with the key backed up separately, or client-side encryption of each dump (age, a public key in the CronJob); MinIO SSE on `backups`.
Standard: §13, §10.2, §10.4; OWASP A04:2025; CWE-311
```

```
[SEVERITY: Medium] MinIO root password in plain env values of Job specs
Where: infra/terraform/minio.tf:181; intake_api.tf:54,78; mcp.tf:90,114; backups.tf:47
Risk: MC_HOST_local carries the root password in the Job and Pod spec, not a Secret; anyone who can get or describe Jobs or Pods reads it.
Fix: Put the MC_HOST_local URI in a Secret and use value_from.secret_key_ref.
Standard: §16.4, §3.1; CWE-526
```

```
[SEVERITY: Medium] The audit log can be rewritten by the login every task uses
Where: dip/sql/warehouse.sql:312-314, 346-351
Risk: `warehouse` owns ops.audit_log and is in every Airflow pod; as owner it can disable the append-only trigger and edit or delete rows. The file's own comment names this as a follow-up, but nothing tracks it.
Fix: A separate owner role for the ops schema (fits 14.3's migrations); writers get INSERT only; the app connects as a non-owner.
Standard: §12.2, §16.4; OWASP A09:2025; CWE-284
```

```
[SEVERITY: Medium] No pod security baseline on the core services
Where: infra/terraform/postgres.tf:106, minio.tf:50, sftp.tf (no security_context); namespaces in cluster.tf:55-67
Risk: Postgres, MinIO, SFTP and the setup Jobs run with default capabilities and a writable root filesystem, and no Pod Security Admission label enforces anything.
Fix: `pod-security.kubernetes.io/enforce: restricted` per namespace (baseline for SFTP, which needs root to chroot); runAsNonRoot, drop ALL, seccomp RuntimeDefault, read-only root filesystem where it works.
Standard: §16.4; CWE-250
```

```
[SEVERITY: Medium] No NetworkPolicies
Where: infra/terraform/*.tf (none)
Risk: Any pod reaches Postgres, MinIO, SFTP, the MCP server and the unauthenticated metrics endpoints (dip/metrics.py:293), so a compromised task pod moves sideways freely.
Fix: Default-deny ingress per namespace, then the known flows. Check with a test pod that kind's CNI enforces them (install one that does, or document it).
Standard: §16.4, §8; OWASP A01:2025; CWE-284
```

```
[SEVERITY: Medium] The triage agent sees unmasked PHI columns
Where: agents/policies/triage.yaml (no `mask_phi: true`; every other agent policy has it)
Risk: Rejected rows' PHI values reach the model, and the Gemini free-tier fallback. On a MIMIC-backed warehouse that breaks §10.4 and PhysioNet's rule against sending its data to online LLMs. Today's data is synthetic.
Fix: `mask_phi: true` for triage (it needs codes, not values), or refuse live calls unless the warehouse is synthetic-only. No live fallback to a free tier for any step that sees PHI.
Standard: §10.4, §14.1; LLM02; CWE-359
```

```
[SEVERITY: Medium] No alerts on security events
Where: observability/prometheus/rules.yml; dip/intake_api.py:95-103 (auth failures not logged)
Risk: Denied agent calls, intake API 401s and rejected approvals raise nothing, and no alert reaches anyone until Alertmanager exists (16.3).
Fix: Export dip_audit_denied_total and intake auth-failure counters; spike rules with promtool tests; route them through 16.3's receiver.
Standard: §12.1, §12.3; OWASP A09:2025; CWE-778
```

### Low

```
[SEVERITY: Low] No dependency audit of the Python environment; the dev extra pins Pillow 12.3.0
Where: .github/workflows/ci.yml (no pip-audit); pyproject.toml:36
Risk: The CI and dev environments are not scanned (Trivy covers images only), and the dev extra pins the Pillow version whose CRITICAL CVEs 12.9 fixed in the OCR image.
Fix: A pip-audit step that fails on findings; Pillow 13.0.0 in the dev extra.
Standard: §4.3, §18
```

```
[SEVERITY: Low] No gitleaks, in pre-commit or in CI
Where: .githooks/pre-commit:4; .github/workflows/security.yml
Risk: Push protection covers provider token formats only; a de-id key, Fernet key or password in a commit would not be caught.
Fix: gitleaks (pinned) in the pre-commit hook and a CI step with `--redact`; one full-history scan.
Standard: §3.3, §19.3; CWE-798
```

```
[SEVERITY: Low] No zizmor scan of the workflows
Where: .github/workflows/
Risk: Workflow injection and permission mistakes are only partly covered by CodeQL's actions pack.
Fix: `zizmor .github/workflows` in the security workflow.
Standard: §16.5, §18
```

```
[SEVERITY: Low] Some images are pinned by tag, not digest
Where: images/airflow/Dockerfile:3 (apache/airflow:3.4.0-python3.12); infra/terraform/variables.tf:85,123,129 (postgres, marquez, marquez-web)
Risk: A retagged upstream image changes what is built or run; the OCR image inherits the Airflow base.
Fix: `@sha256:` pins; Dependabot keeps them current.
Standard: §4.4; CWE-494
```

```
[SEVERITY: Low] Dependabot has no explicit cooldown
Where: .github/dependabot.yml
Risk: Updates are proposed after GitHub's 3-day default, shorter than the standard's 7.
Fix: `cooldown: { default-days: 7 }` per ecosystem (https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference).
Standard: §4.6, §16.5
```

```
[SEVERITY: Low] No SECURITY.md, and private vulnerability reporting is off
Where: repo root; repo settings (private-vulnerability-reporting: false)
Risk: An outside reporter has no private channel, so a report may land as a public issue.
Fix: (owner) Turn on private vulnerability reporting; a SECURITY.md that points to it, with no personal email (CLAUDE.md).
Standard: §19.3
```

```
[SEVERITY: Low] Expressions inlined into a `run:` script in the release workflow
Where: .github/workflows/release.yml:94
Risk: `${{ secrets.GITHUB_TOKEN }}` and `${{ github.actor }}` are interpolated into the shell. The job runs only on a tag push, so the risk is small.
Fix: Pass both through `env:` and quote them.
Standard: §16.5; CWE-78
```

```
[SEVERITY: Low] .gitignore misses some credential patterns
Where: .gitignore:1-6
Risk: `*.p12`, `secrets/` and credential JSON files (`*credentials*.json`, `*-sa.json`) are not ignored.
Fix: Add them; and `.claude/worktrees/`, which agent worktrees create inside the repo today.
Standard: §3.1; CWE-538
```

```
[SEVERITY: Low] Credential masking in task logs covers passwords only
Where: dip/agents/read_tools.py:169-178 (_CREDENTIAL)
Risk: Bearer tokens, `sk-ant-` and `AIza` keys, AWS keys and `secret=` pairs in a task log reach the model unmasked.
Fix: Extend the pattern to tokens, API keys, Authorization headers and known key prefixes, with unit tests.
Standard: §10.3; LLM02
```

```
[SEVERITY: Low] Model-written Markdown goes into a PR body unsanitized
Where: dip/agents/contract_pr.py:163,179 (_pr_body: rationale, review reasons)
Risk: Once a person runs `gh pr create`, an injected image or link renders on GitHub: an exfiltration or phishing channel.
Fix: Strip Markdown images and links and escape HTML in model text, or fence it in a code block.
Standard: §14 (LLM05: improper output handling)
```

```
[SEVERITY: Low] Evals test injection into actions, not exfiltration; the reviewer has no injection case
Where: agents/evals/*/cases.yaml; agents/evals/reviewer/cases.yaml
Risk: Nothing checks that injected text cannot steer record values or keys into message_to_vendor, rationale or diagnosis, or that the reviewer resists an instruction inside the author's rationale.
Fix: Canary cases scored on the canary's absence from free text; a reviewer injection case. New cases need recordings: the owner's OK (cost rule).
Standard: §15.2 evals; LLM01; ASI01
```

```
[SEVERITY: Low] MCP keys are static and never rotated
Where: dip/agents/mcp_server.py:154-171
Risk: Keys never expire and have no audience; a leaked key works until someone notices.
Fix: A rotation schedule (Terraform taint); short-lived tokens with an audience before anything is hosted. (Network reach is the NetworkPolicy item.)
Standard: §15.3; ASI03
```

```
[SEVERITY: Low] Per-agent hourly limits live in memory
Where: dip/agents/registry.py:176
Risk: A crash-looping MCP pod resets the hard per-agent cap.
Fix: Count calls per agent per hour from ops.audit_log.
Standard: §15.1; LLM10
```

```
[SEVERITY: Low] No rate or concurrency limits on the HTTP services
Where: dip/intake_api.py:86-113; dip/agents/gateway.py; the MCP http_app
Risk: A key holder, or any pod for unauthenticated paths, can repeat 20 MB uploads buffered whole in memory, or hammer the token check.
Fix: Per-key and per-IP limits (middleware), uvicorn `--limit-concurrency`, uploads streamed to MinIO. (Backpressure is also 16.1.)
Standard: §8 API4:2023, §16.3; CWE-770
```

```
[SEVERITY: Low] The approvals page sends no security headers
Where: dip/agents/gateway.py:142-144
Risk: No CSP, frame-ancestors, nosniff or Referrer-Policy. The page renders with textContent only, so the risk today is small.
Fix: CSP `default-src 'self'; frame-ancestors 'none'` (moving the inline script to a file or using a nonce), nosniff, no-referrer, Cache-Control: no-store on /api.
Standard: §9, §7.3; OWASP A02:2025; CWE-1021
```

```
[SEVERITY: Low] The intake API serves /openapi.json without a key
Where: dip/intake_api.py:80
Risk: The endpoint list is readable without auth (/docs and /redoc are already off).
Fix: `openapi_url=None`, as gateway.py:126 does.
Standard: §8 API9:2023; CWE-200
```

```
[SEVERITY: Low] Raw exception text goes back to callers and to the model
Where: dip/agents/mcp_server.py:134-135; dip/agents/gateway.py:140,196
Risk: psycopg errors (SQL fragments, role names, sometimes row values) reach the model, skipping PHI masking, and the approvals 503 echoes the database message.
Fix: A generic error with a correlation id; the full error logged on the server only; the deliberate Denied and ApprovalError messages stay.
Standard: §11, §10.3; API8:2023; CWE-209
```

```
[SEVERITY: Low] SFTP sync reads whole files with no size cap and no name check
Where: dip/sftp_sync.py:101-106
Risk: A vendor account, or a stolen vendor key, can drop a multi-GB file that is read whole into the worker's memory; odd names reach landing keys the API channel would refuse.
Fix: Check st_size against the API's limit before reading; apply intake_api.SAFE_NAME and refuse anything else.
Standard: §7.1, §7.9; CWE-400, CWE-20
```

```
[SEVERITY: Low] Two token-protected approvals routes are missing from the negative test
Where: tests/integration/test_gateway.py:153-164
Risk: /api/approvals/{id} and /api/trace are protected, but the "every route needs the token" test does not call them, so losing their dependency would go unnoticed.
Fix: Loop over app.routes and assert 401 for every /api path without the token.
Standard: §6.3; CWE-862
```

```
[SEVERITY: Low] No TLS or encryption at rest inside the cluster
Where: infra/terraform/airflow.tf:214,257 (postgresql:// without sslmode); minio.tf:53 (http)
Risk: Pod-to-pod traffic and volumes are plaintext. Acceptable on a laptop; it matters before anything is hosted.
Fix: A self-signed CA (Terraform tls or cert-manager), Postgres `ssl=on` with `sslmode=verify-full`, MinIO TLS and SSE.
Standard: §10.2; CWE-319
```

```
[SEVERITY: Low] Terraform state holds the model API keys; state and kubeconfig are plaintext
Where: infra/terraform/variables.tf:152,159; versions.tf (local backend); cluster.tf:2
Risk: Local state holds every password, the SFTP keys and the paid Anthropic key, and `.kube/` holds a cluster-admin certificate. Both are gitignored and untracked, but unencrypted on disk.
Fix: Keep model keys out of Terraform (create that Secret with kubectl from the environment), or use OpenTofu state encryption (13.5).
Standard: §16.4, §3.1; CWE-312
```

```
[SEVERITY: Low] No IaC scan in CI
Where: .github/workflows/
Risk: Misconfigurations in infra/terraform and the Helm values are found only by review.
Fix: `trivy config infra/` beside 12.9's image scan.
Standard: §16.4, §18
```

```
[SEVERITY: Low] No retention for logs and data buckets; no second copy of the backups
Where: infra/helm-values/airflow.yaml:58; docs/backups.md:34
Risk: Task logs and landing, raw and quarantine data grow without limit; the backups live in the same MinIO as the data they protect.
Fix: MinIO lifecycle rules for airflow-logs now; per-layer retention is planned in 22.2; optionally a host copy of the newest dump.
Standard: §10.1, §13
```

```
[SEVERITY: Low] No Claude Code project settings
Where: .claude/settings.json (absent)
Risk: Nothing in the repo stops an agent session from reading a credential file or force-pushing; the guardrails live only in prompts and in the user's own settings.
Fix: The settings proposed below, reviewed by the owner.
Standard: §17.2, §19.3
```

## What already meets the standard

- **Secrets:** random 32-character passwords, a random Fernet key, sensitive Terraform outputs, Kubernetes Secrets for credentials, gitignored state and kubeconfig; secret scanning and push protection on.
- **CI:** read-only default token in every workflow, no `pull_request_target`, inputs reach shells through `env:`, Trivy pinned by digest and passing on all four images, CodeQL on Python and the workflows, a kind download checked by SHA-256.
- **Images:** the intake API and MCP images use digest-pinned slim bases and a non-root user; direct dependencies pinned exactly.
- **Network and auth:** NodePorts on 127.0.0.1; per-client and per-agent keys stored as SHA-256 hashes and compared in constant time; authorization before a 404 so feed names cannot be probed; DNS-rebinding and Host checks on MCP and the approvals page; CSRF closed by JSON plus a custom header; two-account tests for agents and intake clients.
- **Input:** a strict filename rule, size caps checked twice, conditional writes; parsers capped for size, pages and cells, with macros refused and PDF decompression bombs refused (12.8); YAML with safe_load; every SQL value parameterized, identifiers from fixed lists or a strict regex; subprocesses with argument lists, no shell with untrusted input.
- **Agents:** deny-by-default policies enforced on the server; every write needs a human approval, re-checked under a row lock; an append-only audit log with argument hashes; vendor content marked untrusted; PHI masked for every role but triage; every tool output size-limited; no NL-to-SQL; no model output executed; injection evals replayed in CI.
- **Database:** separate NOINHERIT logins with statement timeouts and column grants; the cohort reader revoked on MIMIC builds.
- **Kubernetes and SFTP:** RBAC limited to a namespaced read of pods and events; the intake API, MCP, approvals and metrics pods run non-root with a read-only root filesystem; SFTP is key-only and chrooted, with a pinned host key.
- **Backups and data guards:** nightly dumps with SHA-256 checks and a restore drill on every push (14.1, 14.2); a MIMIC and data-file guard in pre-commit and CI.

## Proposed `.claude/settings.json` (Section 17.2, tailored)

The settings syntax was checked against the current Claude Code docs on 2026-11-10:
- https://code.claude.com/docs/en/permissions.md
- https://code.claude.com/docs/en/settings.md
- https://code.claude.com/docs/en/permission-modes.md
- https://code.claude.com/docs/en/tools-reference.md

Not committed: security settings are the owner's call (24.42).

```json
{
  "permissions": {
    "defaultMode": "default",
    "disableBypassPermissionsMode": "disable",
    "deny": [
      "Read(**/.env)", "Read(**/.env.*)", "Read(**/secrets/**)",
      "Read(**/*.pem)", "Read(**/*.key)", "Read(**/*.p12)",
      "Read(**/*.tfstate)", "Read(**/*.tfstate.*)",
      "Read(**/.kube/**)", "Read(**/kubeconfig*)",
      "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/gcloud/**)",
      "Read(~/.kube/**)", "Read(~/.npmrc)", "Read(~/.pypirc)",
      "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)",
      "Bash(git push * -f*)", "Bash(git push --delete *)", "Bash(git push * --delete *)",
      "Bash(git push * --tags*)", "Bash(git branch -D *)",
      "Bash(gh pr merge * --delete-branch*)", "Bash(gh release *)", "Bash(gh repo delete *)",
      "Bash(rm -rf *)", "Bash(sudo *)",
      "PowerShell(git push --force*)", "PowerShell(git push -f*)",
      "PowerShell(git push * --force*)", "PowerShell(git push * -f*)",
      "PowerShell(Remove-Item * -Recurse*)", "PowerShell(Get-Content *.env*)"
    ],
    "ask": [
      "Bash(pip install *)", "Bash(uv add *)", "Bash(uv pip install *)",
      "Bash(npm install *)", "Bash(npm i *)", "Bash(pnpm add *)", "Bash(npx *)",
      "Bash(terraform apply *)", "Bash(terraform destroy *)",
      "Bash(kubectl delete *)", "Bash(kubectl apply *)",
      "Bash(helm install *)", "Bash(helm upgrade *)",
      "Bash(docker run *)", "Bash(wget *)",
      "PowerShell(pip install *)", "PowerShell(terraform apply *)",
      "PowerShell(kubectl delete *)",
      "WebFetch"
    ],
    "allow": [
      "Bash(git status)", "Bash(git diff *)", "Bash(git log *)",
      "Bash(git push)", "Bash(git push -u origin *)",
      "Bash(gh pr checks *)", "Bash(gh pr view *)", "Bash(gh run view *)",
      "Bash(pytest *)", "Bash(ruff check *)", "Bash(ruff format --check *)",
      "WebFetch(domain:docs.github.com)", "WebFetch(domain:code.claude.com)",
      "WebFetch(domain:docs.astral.sh)", "WebFetch(domain:pip.pypa.io)",
      "WebFetch(domain:airflow.apache.org)"
    ]
  }
}
```

**The deny list.** The standard's list is kept in full. The changes:
- **Path form.** `./**/x` becomes `**/x`, the docs' form for "any depth". `./` is relative to where Claude was started, not the repo root.
- **More patterns.** Added `*.p12`, `.kube/` and `kubeconfig*`, which this repo uses.
- **Force pushes.** Added `git push * --force*` and `* -f*`: the standard's `git push --force *` does not match `git push origin -f`.
- **Overnight rules.** Added branch deletion (`--delete`, `branch -D`, `gh pr merge --delete-branch`), tag pushes and `gh release`, matching the owner's overnight rules: no branch deletion, no tag or release without him.
- **PowerShell mirrors.** Bash rules do not cover the PowerShell tool on this Windows machine.
- **A side effect.** Any Bash deny rule turns the PowerShell tool off for the session (tools-reference.md). Keeping it needs `CLAUDE_CODE_USE_POWERSHELL_TOOL=1` in the owner's *user* settings; project `env` values apply only once the folder is trusted.

**Changes to the ask list, and why.**
1. **`git push *` moves from ask to allow, for plain branch pushes only** (`git push`, `git push -u origin *`). Every PR here needs a push. In an unattended run an ask stalls in `default` mode, or is refused in `dontAsk`, so the overnight runs could not open a PR. Force pushes, deletions and tag pushes stay denied above. The guard is the deny list plus branch protection (24.6), and that guard does not exist until the owner turns branch protection on.
2. **`curl *` leaves the ask list.** Fetches go through `WebFetch`, allowed only for the documentation domains above and asked for everything else. The docs warn that WebFetch rules do not stop curl, and that prefix rules are guardrails, not a boundary. The real boundary is the sandbox (17.1), which on Windows needs WSL2 or a dev container.
3. **`supabase db push` and `vercel *` are removed.** This repo uses neither.
4. **Installs, `terraform apply`/`destroy`, `kubectl apply`/`delete`, `helm`, `docker run` and `wget` stay on ask.** Directive 11 (no new dependency without the owner) and directive 13 (no destructive commands) need a human. CLAUDE.md treats the throwaway kind clusters as normal work, but those run inside CI and `scripts/up.sh`, not as bare commands. An unattended run that hits an ask writes the reason under its backlog item and moves on, as CLAUDE.md says.
5. **`defaultMode` stays `default`, not `dontAsk`.** `dontAsk` would refuse every ask silently in interactive sessions too. For overnight runs, `claude -p --permission-mode dontAsk` can be passed per run.

**Notes for the owner.**
- **Folder trust.** A project's `allow` rules are not applied until the folder has been trusted once in an interactive session; deny and ask apply at once.
- **Gitignore first.** `.claude/worktrees/` must be gitignored before `.claude/` is committed (24.26).

## Backlog items added

24.2 to 24.42 in `docs/BACKLOG.md`, one per finding, in severity order. Five need the owner,
in GitHub settings or for spend: 24.6, 24.7, 24.24, the Console spend limit in 24.3, and
the recordings in 24.29.
