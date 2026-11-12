# A three-minute walkthrough (23.2)

A script for recording one take of the platform, for a hiring manager who has five minutes.
It shows one bad file going through the whole platform: detected, quarantined, diagnosed by
an agent, fixed by a person, and the good data reaching gold. Everything shown is synthetic.

**Before recording** (not on camera, about 15 minutes):

```bash
TF_VAR_profiles='["core", "observability"]' bash scripts/up.sh   # the stack on kind
bash scripts/smoke.sh                                             # everything answers
tf() { terraform -chdir=infra/terraform "$@"; }
tf output -raw airflow_url                                        # Airflow, already on a host port
kubectl -n dip-data port-forward svc/approvals 8081 &
kubectl -n dip-obs port-forward svc/grafana 3000:80 &              # user admin
kubectl -n dip-obs port-forward svc/marquez-web 3001:3000 &        # lineage
tf output -raw approvals_token                                    # paste into the page once
# for `dip land`: MinIO on its host port, as root (a laptop-only demo cluster)
export DIP_S3_ENDPOINT="$(tf output -raw minio_api_url)"
export DIP_S3_ACCESS_KEY="$(tf output -raw minio_root_user)" DIP_S3_SECRET_KEY="$(tf output -raw minio_root_password)"
```

Two ways to show the agents. **Recorded, $0** (the default): scene 3 runs `scripts/demo.sh`,
which replays the agents' recorded turns through the real MCP server, policies and graph. **Live**
(costs a few cents, so ask the owner first): pass a model key to `scripts/up.sh`, and the
`agent_chain` DAG diagnoses the incident in the cluster on its own within five minutes.

Open these browser tabs in advance: Airflow (the `airflow_url` above), the approvals page
(`http://127.0.0.1:8081`), Grafana's Feeds dashboard (`http://127.0.0.1:3000`) and Marquez
(`http://127.0.0.1:3001`).

## Scene 1, 0:00 to 0:30: a vendor sends a broken file

**Show** a terminal.

```bash
B=tests/fixtures/eligibility/good/elig_good.csv
python -m dip break rename_column $B /tmp/elig_20261112.csv --arg column=plan_code --arg to=plan
python -m dip land healthcare.eligibility /tmp/elig_20261112.csv
```

Then, in Airflow, unpause `intake__healthcare__eligibility` (every DAG starts paused) and
press **Trigger**.

**Say:** "A vendor renamed a column without telling anyone. The file lands in MinIO, the
platform copies it, untouched, to a raw bucket keyed by its hash, and checks it against the
feed's data contract."

## Scene 2, 0:30 to 1:00: quarantine with a reason

**Show** Airflow: the `intake__healthcare__eligibility` run, red on the intake task, and its
log (the screenshot `docs/images/quarantine-reason.png` shows the same view).

**Say:** "The contract caught it: a missing required column. The whole file goes to
quarantine with a machine-readable reason code, nothing reaches the warehouse, and an incident
opens. Grafana's Feeds dashboard shows the quarantine rate jump." Switch to Grafana for five
seconds.

## Scene 3, 1:00 to 1:50: an agent diagnoses, and asks

**Show** a terminal running `bash scripts/demo.sh` (recorded) and let it scroll; or, live, the
approvals page once the proposal appears.

**Say:** "An incident agent reads the incident through read-only tools: the quarantine rows,
the contract, the task log, the runbooks. It names the cause and hands a typed diagnosis to a
triage agent, which proposes the fix: ask the vendor to resend. It cannot do it itself. Its
first attempt to act is refused, and that refusal is in the audit log too."

## Scene 4, 1:50 to 2:30: a person approves

**Show** the approvals page: the pending proposal, its payload, the agent and model that made
it. Click **Approve** (enter your name as Reviewer).

**Say:** "A person decides. Each reviewer has their own token, so the decision is recorded under
their name. Only now can the agent carry out exactly the action that was approved: same
payload, unexpired, its hash checked."

Then the audit tab, filtered to the chain: every tool call, allowed or denied, in order
(`docs/images/approvals-audit.png`).

## Scene 5, 2:30 to 3:00: the fix lands

**Show** a terminal: the vendor's corrected file landed, and the gold table.

```bash
cp tests/fixtures/eligibility/good/elig_good.csv /tmp/elig_20261112.csv   # the vendor's resend
python -m dip land healthcare.eligibility /tmp/elig_20261112.csv
```

Trigger the DAG again, then show the run turning green, and Marquez's lineage graph from gold back to the file
(`docs/images/marquez-lineage.png`).

**Say:** "The corrected file passes, dbt builds silver and gold and runs its tests, and the
lineage shows exactly which file every gold row came from. The whole thing runs on Kubernetes
locally, built by Terraform, and the same end-to-end test runs on every push."

## After recording

Upload the video wherever the owner keeps it, and put its link in the README's "What it looks
like" section next to the screenshots.
