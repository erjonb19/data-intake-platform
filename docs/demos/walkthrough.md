# A three-minute walkthrough (23.2)

A script for recording the platform for a hiring manager who has five minutes. It follows one
bad file through the platform: detected, quarantined with a reason, diagnosed by an agent,
approved by a person, and the corrected file reaching gold. Everything shown is synthetic.

Record on a **fresh cluster**: a second take on the same one lands the corrected file as a
duplicate of the first (same SHA-256), and it does not reach gold again.

## Before recording (not on camera, about 15 minutes)

```bash
TF_VAR_profiles='["core", "observability"]' bash scripts/up.sh   # the stack on kind
tf() { terraform -chdir=infra/terraform "$@"; }
export KUBECONFIG="$(tf output -raw kubeconfig_path)"
bash scripts/smoke.sh                                             # everything answers
bash scripts/creds.sh                                             # the Airflow login
tf output -raw grafana_admin_password                             # Grafana, user admin
tf output -raw airflow_url                                        # Airflow, already on a host port
kubectl -n dip-data port-forward svc/approvals 8081 &
kubectl -n dip-obs port-forward svc/grafana 3000:80 &              # user admin
kubectl -n dip-obs port-forward svc/marquez-web 3001:3000 &        # lineage
tf output -raw approvals_token                                    # paste into the page once
# for `dip land`: MinIO on its host port, as root (a laptop-only demo cluster)
export DIP_S3_ENDPOINT="$(tf output -raw minio_api_url)"
export DIP_S3_ACCESS_KEY="$(tf output -raw minio_root_user)" DIP_S3_SECRET_KEY="$(tf output -raw minio_root_password)"
# today's date in the file name: the eligibility contract refuses one more than 7 days old
F="/tmp/elig_$(date +%Y%m%d).csv"
```

Open these browser tabs: Airflow (the `airflow_url` above), the approvals page
(`http://127.0.0.1:8081`), Grafana's Feeds dashboard (`http://127.0.0.1:3000`) and Marquez
(`http://127.0.0.1:3001`).

**The agents: choose one.** Scenes 3 and 4 differ:

- **Recorded, $0 (the default).** `scripts/demo.sh` replays the agents' recorded turns through
  the real intake, MCP server, policies and graph, on a throwaway Postgres with its own invented
  file. It is a separate cut, not this cluster's incident.
- **Live, in this cluster** (a model call costs a few cents: ask the owner first). Pass a model
  key to `scripts/up.sh` (`TF_VAR_anthropic_api_key`) and unpause the `agent_chain` DAG in
  Airflow before scene 1; it runs every 5 minutes and diagnoses the incident scene 2 opens.

## Scene 1, 0:00 to 0:30: a vendor sends a broken file

**Show** a terminal.

```bash
python -m dip break rename_column tests/fixtures/eligibility/good/elig_good.csv "$F" \
  --arg column=plan_code --arg to=plan
python -m dip land healthcare.eligibility "$F"
```

Then, in Airflow, unpause `intake__healthcare__eligibility` (every DAG starts paused) and press
**Trigger**.

**Say:** "A vendor renamed a column without telling anyone. The file lands in MinIO, the
platform copies it, untouched, to a raw bucket keyed by its hash, and checks it against the
feed's data contract."

## Scene 2, 0:30 to 1:00: quarantine with a reason

**Show** the run in Airflow and the intake task's log: the file is `quarantined` with two
reason codes, `SCHEMA.MISSING_COLUMN:plan_code` and `SCHEMA.UNEXPECTED_COLUMN:plan` (the
contract allows no extra columns). The run itself succeeds: a quarantine is an outcome the
pipeline handles, not a crash. Then Grafana's Feeds dashboard, "Files quarantined whole (24 h)",
for five seconds.

**Say:** "The contract caught it: a required column is missing. The whole file goes to
quarantine with a machine-readable reason, nothing reaches the warehouse, and an incident opens
for the feed."

## Scene 3, 1:00 to 1:50: an agent diagnoses, and asks

**Recorded:** run `bash scripts/demo.sh` and let it scroll. **Live:** wait for the
`agent_chain` run, then open the approvals page.

**Say:** "An incident agent reads the incident through read-only tools: the quarantine rows,
the contract, the task log, the runbooks. It names the cause and hands a typed diagnosis to a
triage agent, which proposes the fix: ask the vendor to resend. It cannot carry that out by
itself." Recorded only: "Its first attempt to act before anyone approved is refused, and the
refusal is in the audit log too."

## Scene 4, 1:50 to 2:30: a person approves

**Live:** the approvals page shows the pending proposal, its payload, and the agent and model
that made it. Click **Approve** with your own name as Reviewer. **Recorded:** the demo approves
its proposal itself and prints the audit trail; show the approvals page from
`docs/images/approvals-queue.png` while you speak.

**Say:** "A person decides. Each reviewer has their own token, so the decision is recorded under
their name. Only then can the agent carry out exactly the action that was approved: the same
payload, unexpired, its hash checked."

Then the audit log, filtered to the chain: every tool call, allowed or denied, in order (live:
the page's audit tab; recorded: `docs/images/approvals-audit.png`).

## Scene 5, 2:30 to 3:00: the fix lands

**Show** a terminal: the vendor's corrected file, under the same name.

```bash
cp tests/fixtures/eligibility/good/elig_good.csv "$F"
python -m dip land healthcare.eligibility "$F"
```

Trigger the DAG again: this time the intake task's log shows the file `loaded`, and `transform`
builds silver and gold. Then Marquez's lineage graph from gold back to the file
(`docs/images/marquez-lineage.png` shows the same view).

**Say:** "The corrected file passes, dbt builds silver and gold and runs its tests, and the
lineage shows which file every gold row came from. All of it runs on Kubernetes locally, built
by Terraform, and the same end-to-end test runs on every push."

## After recording

Upload the video wherever the owner keeps it, and put its link in the README's "What it looks
like" section next to the screenshots.
