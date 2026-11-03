# Stage 11 demo: the platform breaks, the incident agent diagnoses it

The stage's proof, on 2026-11-03, in the manual `proof` workflow
(`.github/workflows/proof.yml`, job `stage-11-proof`, run 41611889836) on GitHub runners,
from `s11-10-proof`. Two jobs ran in the one dispatched run, and both passed. Every input
was synthetic.

```bash
gh workflow run proof.yml --ref s11-10-proof -f job=stage-11-proof -f spend_cap_usd=0.15
```

The run's results were committed to the branch by the workflow's `commit-results` job, after
its key and auth-header check: [`docs/demos/proof/run-41611889836/results.md`](proof/run-41611889836/results.md)
and `results.json`.

## The drills, with the incident agent live (`drills`)

`up.sh` with the core and observability profiles (no model key), `smoke.sh`, then
`scripts/proof_drills.py`. For each drill in turn:

1. `dip drill NAME` broke one thing in the kind cluster.
2. The platform check (its CronJob, run now as a Job) ran until the drill's incident opened.
3. The drill's alert was seen firing in Prometheus.
4. The incident chain ran once, in the cluster, on that incident only. It ran as a pod of the
   platform image: `dip.agents.chain_job --limit 1 --incident ID`, with the agent_chain
   DAG's Secret plus a model-key Secret made for the run and deleted after. Each chain had a
   quarter of the $0.15 cap. During `bad_secret` the pod reached the warehouse with the
   password the drill had saved, because the Secret held the wrong one by design.
5. `dip drill NAME --undo`, then the check ran until the incident closed.

| Drill | What broke | Incident | Alert | Live diagnosis (expected) | Model calls | Cost | Trace spans | Closed |
|---|---|---|---|---|---|---|---|---|
| `scheduler_down` | the scheduler Deployment scaled to 0 | `PLATFORM.SCHEDULER_DOWN` | AirflowSchedulerDown | `platform_scheduler` (same) | 5 | $0.0050 | 11 | yes |
| `oom_task` | a task past its 128Mi limit | `TASK.OOMKILLED` `drill_oom.eat_memory` | TaskPodOOMKilled | `platform_resources` (same) | 6 | $0.0052 | 14 | yes |
| `bad_secret` | a wrong warehouse password in `airflow-connections` | `DAG.FAILED` `intake__healthcare__eligibility` | DagRunFailing | `platform_credentials` (same) | 7 | $0.0056 | 13 | yes |
| `stuck_queued` | a task asking for 256Gi, its pod `Pending` | `TASK.STUCK_QUEUED` `drill_queue.wait_for_room` | TaskQueuedTooLong | `platform_capacity` (same) | 5 | $0.0044 | 11 | yes |

Four of four live diagnoses named the expected root cause, for **$0.0201** in all (metered
from `ops.model_call`, the same meter as the caps). The trace spans are the chain's model and
tool spans in `ops.trace_span`.

## The fresh-clone E2E (`fresh-clone`), closing Stage 8's open check

The job made a clean checkout in a new directory. It then ran the README's quick-start
commands as the README has them: read from `README.md` at run time, so they cannot drift,
with `down.sh` (the README's last command) held until the end. The whole E2E followed,
including the break loop and the four drills. It passed in this run, and in the two before it
(41599923578, 41605802597). Two departures from "verbatim":

- **The environment:** `TF_VAR_profiles='["core", "observability"]'`, which the README's quick
  start documents for the full check. Without it, the E2E skips its observability and lineage
  stages and the drills' alert checks.
- **`creds.sh`'s output goes to a file,** so the run's throwaway passwords stay out of the
  public log.

## What the proof found, and fixed before it passed

- **The platform check failed on a fresh stack** with `permission denied for schema ops`.
  `postgres-init.sh` creates an empty `ops` schema, and its tables and every login's grants
  came from `Warehouse.init`, which ran first on an agent_chain or intake DAG run. Until then
  the check could record nothing. At worst, a scheduler that never started could never be seen
  as down. The E2E never showed it, because there the drills run after many intake runs. A
  Terraform Job, `warehouse-init`, now applies the schema and grants at deploy: after the
  roles exist and before the platform-check CronJob.
- **A failed check Job only timed out:** a Job with no retries and a 90 s deadline never
  completes, so `kubectl wait --for=condition=complete` said nothing. The drills' check helper
  now waits for success or failure, prints a failed run's conditions, pod state and log, and
  tries once more.
