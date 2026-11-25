# Owner steps

Everything that needs the owner by hand: commands on the laptop, GitHub or Console settings,
and recordings. One section per step. Commands are for **Windows PowerShell**, run from the
repo folder (the clone of `data-intake-platform`). No secret is ever written here: a
command that needs one reads it from Terraform or prompts for it.

Claude adds a step here whenever one comes up and ticks it when the owner says it is done
(CLAUDE.md, "Owner steps"). Open steps come first; the walkthrough recording is last.

## Open

### [ ] Decide: how long Airflow task logs are kept (24.41)

**Why.** Airflow writes every task's log to the MinIO bucket `airflow-logs`, and nothing ever
removes them, so the bucket grows without limit. The audit (Low) asks for a lifecycle rule. A
rule that expires objects deletes data, so it is your call (CLAUDE.md: ask before deleting
data). Nothing is added until you decide.

**Proposal.** One MinIO lifecycle rule on `airflow-logs` only: expire objects **30 days**
after they were written. Set by Terraform with `mc ilm rule add --expire-days 30`, in the Job
that creates the buckets, so a fresh cluster gets it too.
- **What it deletes:** Airflow task logs (each task try's stdout and stderr) older than 30
  days. Airflow's UI then shows "log not found" for those old tries.
- **What it never touches:** `landing`, `raw`, `quarantine` (the files and what went wrong
  with them), `backups` (already pruned to the newest 7 dumps by the backup job), the
  warehouse, and `ops.audit_log`.
- Why 30 days: an incident is usually looked at within days, so a month leaves margin, and
  the laptop's disk stays small.

The audit's other half, a second copy of the newest backup on the host, deletes nothing; it
comes with Stage 14's restore docs if you want it.

Reply in chat: "24.41 yes, 30 days" (or another number of days), or "no".

### [ ] Decide: SLO targets without Stage 19 (16.4)

**Why.** 16.4 says its targets (the 95th percentile time from landing to gold, the intake
API's availability, each feed's quarantine rate) come from Stage 19's measured numbers, "not
guessed". Stage 19 is on hold (owner, 2026-11-24), so 16.4 is skipped.

**Claude recommends** building 16.4 on numbers the kind E2E on a GitHub runner can measure
(landing-to-gold time per feed and the quarantine rates of its cases, added to the E2E's
report). Each target would be
marked "provisional, from the E2E on a GitHub runner" until Stage 19 measures at scale. Or wait
for Stage 19.

Reply in chat: "16.4 provisional" or "16.4 waits".

### [ ] Decide: how `dip forget` and per-layer retention delete data (22.2)

**Why.** 22.2 deletes data twice over: `dip forget --member <id>` on request, and a scheduled
purge per layer. Both are yours to decide (CLAUDE.md: ask before deleting data). Three things
in the platform make "no trace of the member remains" a design choice, not only code:
- **Raw copies** (`raw/<feed>/<day>/<sha256>`) are whole vendor files, many members each,
  named by their hash. Removing one member means rewriting the file under a new hash, and
  bronze, `ops.intake_file` and quarantine all point at the old one.
- **Backups** (the newest 7 nightly dumps in `backups`) hold the member until they age out.
- **Research extracts** (`data/extracts/<version>/`) are immutable by design.

**Claude recommends:**
- `forget` deletes the member's rows from bronze, quarantine (the `.rejected.ndjson` lines,
  too) and every gold, OMOP and shareable table. It rewrites each raw file that holds the
  member without its rows, with the old hash and the new one recorded in a new
  `ops.forget_request` table. It deletes any extract version that holds the member's
  pseudonym. One `ops.audit_log` row is written per request, with the member id hashed,
  never in clear.
- Backups are not edited: the request is done when the last dump older than it has been
  pruned (7 days), and `dip forget --status` says so.
- Per-layer retention: no scheduled purge until you name the periods (for example raw 1
  year, quarantine 90 days). Until then the setting is empty and the purge does nothing.

Reply in chat: "22.2 as recommended, retention <periods or none>", or what to change.

### [ ] Make pip-audit, gitleaks and zizmor required checks (after 24.19 to 24.21 merge)

**Why.** The three new jobs in the `security` workflow run on every PR, but only required
checks block a merge. Branch protection is a repository setting, so it is yours to change.
Do it once all three have passed on `main`.

**Do.** GitHub, Settings, Branches, the rule for `main`, "Require status checks to pass":
add `pip-audit`, `gitleaks` and `zizmor` beside the six already there (`lint`, `test`,
`e2e`, `trivy`, `analyze (python)`, `analyze (actions)`). Or in PowerShell:

```powershell
gh api repos/erjonb19/data-intake-platform/branches/main/protection/required_status_checks/contexts -X POST -f "contexts[]=pip-audit" -f "contexts[]=gitleaks" -f "contexts[]=zizmor"
```

**Check.**

```powershell
gh api repos/erjonb19/data-intake-platform/branches/main/protection/required_status_checks --jq '.contexts'
```

lists the three with the others.

### [ ] Decide: where signed webhooks are audited (16.2)

**Why.** 16.2 asks for an HMAC-signed "file ready" endpoint on the intake API, with every call
audited. The audit trail is `ops.audit_log` in Postgres, but since 24.16 the intake API cannot
reach Postgres at all (its NetworkPolicy, by design: it only writes files to MinIO). Auditing
there means opening that path, so it is a least-privilege trade-off for you to make.

**Options:**
- **a.** A new login, `dip_webhook`, that may only INSERT into a new `ops.webhook_event`
  table (append-only, like the audit log), and one more NetworkPolicy flow from the intake API
  to Postgres for it. A webhook then triggers the feed's intake DAG through the api-server
  (one more flow), or an Airflow sensor picks the row up.
- **b.** No database: each accepted or refused call is written as a small JSON object in
  MinIO (`landing/_webhooks/...`), which the next `sftp_sync` run reads, audits into
  `ops.audit_log` and acts on. No new network flow, but up to one `sftp_sync` interval of delay.

**Claude recommends b**: it keeps 24.16's boundaries, and the delay matches how files arrive
today.

Reply in chat: "16.2 a" or "16.2 b".

### [ ] Last: record the three-minute walkthrough (23.2)

**Why.** A short video of one bad file going through the platform (detected, quarantined,
diagnosed by an agent, approved by a person, fixed and reaching gold) is the quickest way for
a hiring manager to see the project. The script, scene by scene, is
`docs/demos/walkthrough.md`; below are the same commands for PowerShell. Record it after
Thanksgiving (owner, 2026-11-23), when everything else is finished.

**Before you start.**
- Docker Desktop running, and `kind`, `terraform`, `kubectl` and `helm` on the PATH. Check:
  `& "$env:ProgramFiles\Git\bin\bash.exe" scripts/prereqs.sh`.
- Memory: the whole platform peaked at 5.0 GiB in CI, but a kind node next to a full desktop
  has run this 16 GB laptop out of memory before. Close other apps first; if it still runs
  out, tell Claude.
- The repo's scripts are bash. Windows has no bash by default, but Git for Windows ships
  one, and the commands below call it directly.
- Record on a **fresh cluster**: a second take on the same one lands the corrected file as a
  duplicate, and it does not reach gold again.

**1. Bring the stack up (not on camera, about 15 minutes).**

```powershell
$bash = "$env:ProgramFiles\Git\bin\bash.exe"
function tf { terraform -chdir=infra/terraform @args }
$env:TF_VAR_profiles = '["core", "observability"]'
& $bash scripts/up.sh
$env:KUBECONFIG = tf output -raw kubeconfig_path
& $bash scripts/smoke.sh      # everything answers
& $bash scripts/creds.sh      # the Airflow login
tf output -raw airflow_url    # Airflow, already on a host port
```

**2. Open the other pages (port-forwards in the background).**

```powershell
$kc = $env:KUBECONFIG
Start-Job -ArgumentList $kc { param($kc) kubectl --kubeconfig $kc -n dip-data port-forward svc/approvals 8081 }
Start-Job -ArgumentList $kc { param($kc) kubectl --kubeconfig $kc -n dip-obs port-forward svc/grafana 3000:80 }
Start-Job -ArgumentList $kc { param($kc) kubectl --kubeconfig $kc -n dip-obs port-forward svc/marquez-web 3001:3000 }
tf output -raw grafana_admin_password    # Grafana, user admin
tf output -raw approvals_token           # paste into the approvals page once; Reviewer: owner
```

Browser tabs: Airflow (the `airflow_url` above), the approvals page `http://127.0.0.1:8081`,
Grafana's Feeds dashboard `http://127.0.0.1:3000`, Marquez `http://127.0.0.1:3001`. Print the
password and the token before you start recording, not on camera.

**3. Set up `dip land` (MinIO on its host port; a laptop-only demo cluster).**

```powershell
$env:DIP_S3_ENDPOINT = tf output -raw minio_api_url
$env:DIP_S3_ACCESS_KEY = tf output -raw minio_root_user
$env:DIP_S3_SECRET_KEY = tf output -raw minio_root_password
# today's date in the name: the eligibility contract refuses a file more than 7 days old
$F = Join-Path $env:TEMP "elig_$(Get-Date -Format yyyyMMdd).csv"
```

**4. Record.** Follow `docs/demos/walkthrough.md` for what to show and say. The commands, in
PowerShell:

```powershell
# Scene 1: a vendor sends a broken file (then unpause and trigger
# intake__healthcare__eligibility in Airflow)
.venv\Scripts\python -m dip break rename_column tests/fixtures/eligibility/good/elig_good.csv $F --arg column=plan_code --arg to=plan
.venv\Scripts\python -m dip land healthcare.eligibility $F

# Scene 3 (the recorded agents, $0): let it scroll
& $bash scripts/demo.sh

# Scene 5: the corrected file, same name (then trigger the DAG again)
Copy-Item tests/fixtures/eligibility/good/elig_good.csv $F -Force
.venv\Scripts\python -m dip land healthcare.eligibility $F
```

The live-agent version of scenes 3 and 4 makes model calls (a few cents): ask Claude first.

**5. Afterwards.**

```powershell
Get-Job | Stop-Job; Get-Job | Remove-Job    # the port-forwards
& $bash scripts/down.sh                      # delete the kind cluster
```

Upload the video wherever you keep it and tell Claude the link; it goes into the README's
"What it looks like" section.

## Done

### [x] Decisions for tonight's work (owner, 2026-11-24)

- **24.17:** option a, `mask_phi: true` for triage.
- **24.19, 24.20, 24.21:** add pip-audit, gitleaks and zizmor, in CI only, pinned like the
  other actions (no pre-commit hook).
- **24.29 and the MCP half of 24.35:** yes.
- **Recordings:** one cap of $1.00 for the night across 24.17, 24.29 and 24.35. Claude first,
  then Gemini at $0, in one re-recording pass.
- **24.41:** no delete rule tonight; the proposal is above.

### [x] Settings PR #168 reviewed and merged by the owner (2026-11-23)

The revised allows: `gh pr merge` and pushes to non-main branches allowed, workflow runs on
ask, pushes to `main` denied.

### [x] 24.6 Require a pull request on `main` (2026-11-19)

**Why.** Every change reaches `main` through a pull request, so nobody (Claude included) can
push to it directly and skip the review. No approving review is required (zero approvals),
since Claude merges after its separate-agent review.

**Check.**

```powershell
gh api repos/erjonb19/data-intake-platform/branches/main/protection --jq '.required_pull_request_reviews.required_approving_review_count'
```

It prints `0`.

### [x] Settings PR #165 merged on the owner's word (2026-11-17)

**Why.** From #132's review notes: more denies for pushes to `main` (`x:main`,
`refs/heads/main`, `+main`, `--all`, `--mirror`, `--follow-tags`) and the PowerShell forms of
the Bash denies (`gh release`, `gh repo delete`, `git branch -D` and others). The owner
approved it in chat and asked Claude to merge it.

### [x] Re-recording for runbooks 13 and 14 approved (2026-11-13, confirmed 2026-11-17)

**Why.** Adding runbooks changes what the incident agent searches, so the incident cases are
recorded again (CLAUDE.md, "Model recordings"). Confirmed on 2026-11-17: `claude-recordings`
first under a **$0.10** cap (`spend_cap_usd=0.10`, `DIP_SPEND_CAP_USD`), then
`gemini-recordings` at $0. Claude runs both last on the `s13-3-runbooks` branch and adds the
spend to `docs/PROGRESS.md`.

### [x] 13.5: CI installs OpenTofu with the SHA-pinned action (2026-11-13)

**Why.** The owner chose `opentofu/setup-opentofu`, pinned by commit SHA like every other
action, over the image.

**Check.** `.github/workflows/ci.yml`, the lint job's `OpenTofu init and validate` step.

### [x] Paramiko alert #43 dismissed as inaccurate (2026-11-13)

**Why.** `requirements/e2e.in` pins `paramiko==5.1.*` and the installed lock has 5.1.0 (the
fix is in 5.0.0); GitHub had not re-read the wildcard pin.

**Check.**

```powershell
gh api repos/erjonb19/data-intake-platform/dependabot/alerts/43 --jq '{state, dismissed_reason}'
```

### [x] 12.10 Link the GHCR packages and re-run the release (2026-11-13)

**Why.** The first `v0.12.0` release failed on the GHCR push (`denied` on `dip-airflow`)
because the package was not linked to the repo. Linked and re-run by the owner; `v0.12.0` is
published (the GitHub release and the four images). The re-run needed the repo's SHA-pin rule
for actions off for a moment, since that workflow predates 24.5; it is back on.

**Check.** `git ls-remote --tags origin v0.12.0`, and
`docker pull ghcr.io/erjonb19/dip-airflow:v0.12.0`.

### [x] 24.3 A $5/month spend limit in the Anthropic Console (2026-11-13)

**Why.** The repo caps spend itself (`DIP_MONTHLY_CAP_USD`, counted from `ops.model_call`); a
limit at the provider holds even if that code has a bug. The owner moved the key to its own
`data-intake-platform` workspace with a $5/month limit, deleted the old key and updated the
`ANTHROPIC_API_KEY` GitHub secret.

**Check.** `gh secret list` shows `ANTHROPIC_API_KEY` updated 2026-11-13; the limit is under
the workspace's **Limits** in the Anthropic Console.

### [x] 24.7 Dependabot and private vulnerability reporting (2026-11-11)

**Why.** Alerts and security updates for vulnerable dependencies, and a private channel for
anyone who finds a hole (`SECURITY.md` points to it).

**Check.**

```powershell
gh api repos/erjonb19/data-intake-platform/automated-security-fixes
gh api repos/erjonb19/data-intake-platform/private-vulnerability-reporting
```

Both say enabled.
