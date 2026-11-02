# Runbook 11: a connection refused for its credentials (a bad or rotated secret)

**Signal.** An open `DAG.FAILED` incident for a feed (target the feed's DAG), whose failed
task's log has `password authentication failed for user "<role>"`. Alert
**DagRunFailing**. The password Airflow's pods use (`AIRFLOW_CONN_WAREHOUSE`, from the
`airflow-connections` Secret) no longer matches the one in Postgres: it was rotated in one
place and not the other. Nothing was loaded; the vendor's files wait in landing.

The run's own failure callback may not open the incident (it connects with the same
Secret, when it runs in a pod started after the change); the `platform-check` CronJob,
with its own login, opens it from the Airflow API instead.

**Break it on purpose (kind only):** `python -m dip drill bad_secret` writes a wrong
warehouse password into `airflow-connections` (keeping the real one in an annotation) and
triggers `intake__healthcare__eligibility`; `python -m dip drill bad_secret --undo` puts the
real one back and runs the DAG again. The drill runs in the GitHub proof workflow, never on
the laptop.

**Never repeat a credential**, in a diagnosis, a step, a ticket or a chat, even one a log
shows. Say which login was refused and where its password comes from, not what it is.

## Evidence

1. `get_incident(incident_id)`: the feed, the DAG and the failed run.
2. `get_dag_run(dag_id)` and `get_task_log`: the failed task (usually `intake`, the first
   to connect) and the refused login: `password authentication failed for user
   "warehouse"`. Later tasks are `upstream_failed`.
3. Other feeds' DAGs: if they fail the same way, the shared connection is wrong, not one
   feed.

## Decide

A login refused for its password (`password authentication failed`): which side is wrong.

| What you see | Action |
|---|---|
| The Secret was changed by hand, or a drill left it (`dip.drill/saved-warehouse` annotation on `airflow-connections`) | Put it back from Terraform: `terraform -chdir=infra/terraform apply` rewrites the Secret from its state (for the drill: `dip drill bad_secret --undo`) |
| The password was rotated in Postgres by hand (`ALTER ROLE`), not through Terraform | Rotate through Terraform in both places: replace `random_password.this["pg_warehouse"]`, apply, and set the role's password from the new value; never edit the Secret by hand |
| Only one login is refused, others connect | Check that login's role exists and can log in (`pg_roles.rolcanlogin`); the password is not the cause |
| A connection refused for another reason (`could not connect`, `timeout`) | Not credentials: runbook 09 or a platform failure |

## Check

Long-running pods (scheduler, DAG processor, API server) read the Secret when they start:
after the Secret changes, `kubectl -n dip-airflow rollout restart deployment` for them.
Then rerun the failed DAG run (or trigger the DAG). Its success closes `DAG.FAILED` (its
callback, or the next `platform-check` run within 2 minutes); **DagRunFailing** clears
within the hour.
