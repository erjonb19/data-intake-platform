# Owner steps

Everything that needs the owner by hand: commands on the laptop, GitHub or Console settings,
and recordings. One section per step. Commands are for **Windows PowerShell**, run from the
repo folder (the clone of `data-intake-platform`). No secret is ever written here: a
command that needs one reads it from Terraform or prompts for it.

Claude adds a step here whenever one comes up and ticks it when the owner says it is done
(CLAUDE.md, "Owner steps"). Open steps come first; the walkthrough recording is last.

## Open

### [ ] 24.6 Require a pull request on `main`

**Why.** Branch protection on `main` is on: `lint`, `test`, `e2e`, `trivy` and both CodeQL
analyses must pass, for admins too, and force pushes and deletion are blocked. What 24.6 still
asks is that every change reach `main` through a pull request, so nobody (Claude included)
can push to it directly and skip the review. Claude already works through PRs, so nothing
else changes; no approving review is required (zero approvals), since Claude merges after its
separate-agent review.

**Steps (GitHub).** Repo **Settings**, **Branches**, the rule for `main`, **Edit**: tick
**Require a pull request before merging**, set **Required approvals** to **0**, **Save
changes**.

**Check.**

```powershell
gh api repos/erjonb19/data-intake-platform/branches/main/protection --jq '.required_pull_request_reviews'
```

It prints an object (not nothing) once the rule is on. Tell Claude, and 24.6 is ticked.

### [ ] Merge the settings PR (#165)

**Why.** From #132's review notes, as you chose: more denies for pushes to `main` (`x:main`,
`refs/heads/main`, `+main`, `--all`, `--mirror`, `--follow-tags`) and the PowerShell forms of
the Bash denies (`gh release`, `gh repo delete`, `git branch -D` and others). The
`WebFetch(domain:...)` allows stay. Claude never merges a permission change.

**Steps.** Read the diff, then merge it once CI is green:

```powershell
gh pr diff 165
gh pr merge 165 --merge
```

### [ ] Last: record the three-minute walkthrough (23.2)

**Why.** A short video of one bad file going through the platform (detected, quarantined,
diagnosed by an agent, approved by a person, fixed and reaching gold) is the quickest way for
a hiring manager to see the project. The script, scene by scene, is
`docs/demos/walkthrough.md`; below are the same commands for PowerShell. Record it when
everything else is finished.

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
tf output -raw approvals_token           # paste into the approvals page once
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

### [x] Re-recording for runbooks 13 and 14 approved (2026-11-13)

**Why.** Adding runbooks changes what the incident agent searches, so the incident cases are
recorded again (CLAUDE.md, "Model recordings"). Approved under a $0.25 cap and
`DIP_SPEND_CAP_USD`; Claude runs it last on an `s13-*` branch and adds the spend to
`docs/PROGRESS.md`.

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
