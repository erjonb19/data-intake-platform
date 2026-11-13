# Incidents

Real breaks on this platform, one per stage from Stage 12: what broke, how we knew, how we
fixed it, and what stops it coming back. Drills (`dip drill`, `dip break`) have their own
runbooks in `docs/runbooks/`; this file is for the ones nobody planned. It is not in that
folder on purpose: every `.md` there (except its README) is in the incident agent's runbook
search, and its hits are part of every incident recording's fingerprint.

## 2026-11-03: the nightly evals went red with every case passing (Stage 12, #83)

**What broke.** The nightly `evals` workflow failed three nights running (runs
41631832492, 41827356431, 42027130487). Every one of the seven evals replayed and passed
every case. The last step, "Every case passed", then failed with
`7 reports (expected 6); failed: []`. S11.5 (#73) had added a seventh eval,
`triage-from-rejections`, to the replay step, and nobody changed the hard-coded 6 in the
check below it. The check was right to guard against a missing report; it guarded with a
number that had to be kept in step by hand.

**How we knew.** The S11.12 audit found it (gap 1 in `docs/reports/audit.md`) on
2026-11-03, the day of the first red run. Nothing alerted: a failed scheduled workflow
shows only in the Actions tab and in GitHub's failure email. The run log's last
lines gave the cause directly.

**How we fixed it.** 12.1 (the PR for #83). The replay step's `run()` writes each eval's
name to `reports/expected.txt` as it starts it, and the check is now
`scripts/check_eval_reports.py`: it fails when a started eval wrote no report, when a report
is there that no eval started, when a report has zero cases or cannot be read, or when any
case failed. Adding an eval now needs no other line changed. Unit tests cover each failure,
and one test reads `evals.yml` to check that every eval goes through `run()` and that the
check is the script.

**Check.** Dispatch `evals` on `main` (Actions → evals → Run workflow, or
`gh workflow run evals.yml`): the last step prints
`all 7 evals wrote a report and passed every case`.

**A second break while fixing it.** The first version of this write-up went into
`docs/runbooks/incidents.md`. The proof run on the branch (42139290188) then failed the
`incident` eval: `schema_drift_hands_off_incident.json, turn 4: the agent sent the model
something other than what was recorded`. The new file had changed what `search_runbooks`
returns. Moving it here fixed the replay without re-recording.

**What stops it coming back.** The list of evals is now in one place. CLAUDE.md now
says that any new or changed file
in `docs/runbooks/` means re-recording the incident cases. The missing piece is an
alert when an eval drops: that is Stage 16.3 (Alertmanager with a receiver, and an alert on
the nightly eval result).

## 2026-11-06: e2e on `main` and #107 red, the Airflow chart download timed out (Stage 12)

**What broke.** Every `e2e` run from 13:23 UTC failed before a single test ran: `main`
after #106 merged (run 42225177412), and both runs on #107 twice (42225387574,
42225395837, 42238412980, 42238421200). Terraform failed at `helm_release.airflow` with
`Error locating chart`: it downloaded the official Airflow chart from
`https://archive.apache.org/dist/airflow/helm-chart/1.22.0/airflow-1.22.0.tgz` on every
deploy, and archive.apache.org was throttling (connection reset in one run, client timeout
in another; from a laptop the 291 KB file took 20 s). The same error had hit #105 once on
2026-11-05 and passed on a re-run, so it had been read as a one-off.

**How we knew.** The `e2e` check went red on the PR. The failed step's log named the URL;
the step was "Bring up stack", before any test, and lint and test were green on the same
commits. A fresh review of #107 then pointed out that its new E2E code had never run on a
cluster, so 12.2 was unticked in `docs/BACKLOG.md` until it does.

**How we fixed it.** We removed the network dependency instead of retrying it. The chart is
committed in `infra/helm-charts/` with Apache's `.sha512` and the chart's own `LICENSE` and
`NOTICE`, and `infra/terraform/airflow.tf` deploys the local file. A Terraform
`precondition` refuses a file that does not match the checksum, and
`tests/unit/test_helm_charts.py` checks the same in CI along with the version and the
license copy. The signature was checked once against Apache's KEYS. Updating the version is
written up in `infra/helm-charts/README.md`.

**Check.** The fix's PR: both `e2e` runs green on its final commit, then #107's.
`pytest tests/unit/test_helm_charts.py`.

**What stops it coming back.** Nothing in a deploy fetches the Airflow chart now. The
Prometheus and Grafana charts still come from their GitHub Pages repos in the observability
profile, which every `e2e` run deploys; that CDN has not failed so far, and they move to
the same folder if it does. An alert on a red `main` is still missing (Stage 16.3).

## 2026-11-09: the first backup's E2E hung, and promtool refused the alert (Stage 14)

**What broke.** The first CI run of 14.1 (#118, e2e run 42564406558) took a real backup in
the kind cluster: the Job ran, `ops.backup` row 1 was `ok`, and the 917 KB dump's size and
SHA-256 matched the row. The next check then hung for 300 s and failed: it piped the dump
into `kubectl exec -i postgres-0 -- pg_restore --list`, and `pg_restore` reading a custom
archive from stdin that way never returned. The same run's `lint` job failed too: promtool
refused `rules.yml` with "1 duplicate rule(s) found", because the two `BackupMissing` rules
(a stale backup, and no backup ever) had the same name and the same labels.

**How we knew.** Both jobs went red on the PR. The e2e log ended in
`subprocess.TimeoutExpired ... pg_restore --list ... timed out after 300 seconds`, right
after the checks that passed, and the lint log named the duplicate.

**How we fixed it.** The E2E now writes the dump into the pod and lists it from the file. The
two rules carry `reason: stale` and `reason: none_yet`, which keeps them distinct and says
which case fired; the promtool tests expect those labels. The restore steps in
`docs/backups.md` copy the file in first too, since they had the same stdin pipe.

**What stops it coming back.** The E2E runs a real `pg_restore --list` on every push, and
promtool checks the rules in `lint`. The full restore, timed, is 14.2's drill.

## 2026-11-09: the restore drill's dump arrived in the pod changed (Stage 14, #119)

**What broke.** The first `e2e` run of the 14.2 restore drill (run 42619586313) failed at
`dip restore --latest --verify`. The 919,644-byte dump downloaded from MinIO matched its
`ops.backup` SHA-256 on the runner. It was then piped into `postgres-0` with
`kubectl exec -i postgres-0 -- sh -c 'cat > /tmp/dip-restore.dump'`, and the file in the pod
had a different SHA-256. The table the drill had dropped stayed dropped, and the run failed.
In 14.1, pg_restore reading the same kind of stream from stdin never returned, and
`pg_restore --list` on a piped copy passed only because it reads the archive's table of
contents at the start.

**How we knew.** The restore checks the dump's hash in the pod before it restores anything,
and it stopped with `the copy in the pod has SHA-256 2144c7e0..., not ea8dae7f...`. No
restore ran on a damaged file.

**How we fixed it.** The dump no longer goes through `kubectl exec` stdin. The restore signs
a GET URL for that one object (5 minutes, as the MinIO admin) and passes it on stdin as a
single short line. `postgres-0` downloads the dump from MinIO inside the cluster with
`wget`, and the restore checks the size and SHA-256 in the pod against the row. The manual
steps in `docs/backups.md` use `kubectl cp` and say to check the hash in the pod.

**Check.** The `e2e` run on #119's final commit: `tests/e2e/restore.py` passes, and the
job summary shows the time to restore. `pytest tests/unit/test_restore.py` covers a size
or hash mismatch, which stops before any restore.

**What stops it coming back.** Large files no longer go through `kubectl exec` stdin. The
in-pod size and hash check stays, whatever the transport.

## 2026-11-11: the restore drill "brought the table back" and it was not there (Stage 14)

**What broke.** The `upgrade` workflow's second E2E pass failed three times in a day, on
three unrelated PRs (#136, #137 and #133; runs 42971578210, 42971617128, 43153224864). Its
restore drill printed `brought gold.member_eligibility back into warehouse (2 entries of
the dump)`, and the next query in the live warehouse said `relation
"gold.member_eligibility" does not exist`. Other runs of the same code passed, and the first
pass's drill in the same jobs always passed.

**How we knew.** The upgrade job's FAIL lines, then its diagnostics artifact: the Postgres
log had only the failed lookup, and the Airflow scheduler log showed no DAG running in the
seconds around it, so no dbt run had rebuilt or renamed the table under the drill. That
left the drill's own steps. To bring the table back, `dip/restore.py` wrote the list of the
dump's entries for that table into the pod through `kubectl exec -i`'s stdin, then ran
`pg_restore -L` on it. Nothing checked the list. When that short stdin is lost (`kubectl
exec -i` does this now and then), the file is empty, `pg_restore -L` restores nothing and
exits 0, and the "2 entries" in the message was the drill's own count, not the pod's.

**How we fixed it.** In #133 (14.3), which the failure was holding back. The list goes into
the pod as `printf` arguments, not stdin (its lines are the dump's table of contents,
nothing secret); its lines are counted in the pod before `pg_restore` runs; and the table
must exist afterwards, or the drill fails with that message instead of a confusing one
later. A unit test loses the list on purpose and checks the drill stops. The signed URL the
pod downloads with still goes through stdin, so it never appears in a process list; a lost
URL already fails the size and SHA-256 check that follows.

## 2026-11-13: the approvals gateway's new liveness probe would have restarted it forever (Stage 13, #159)

**What broke.** 13.1 gave every long-running service a liveness probe. The approvals
gateway's was an HTTP check of `/healthz`, which needs no login. But the gateway runs
Starlette's `TrustedHostMiddleware`, which answers 400 to any `Host` header not on its list,
and the kubelet sends the pod's IP as the `Host`. Every check would fail, and the kubelet
would restart the gateway about every 70 seconds (a 10 s delay plus three failures 20 s
apart), for as long as it ran. The readiness probe beside it was TCP for exactly this reason,
and its comment said so.

**How we knew.** Not from CI. The PR's tests and the first E2E were green, because nothing
checked restarts: between restarts every page and API call still worked. The separate-agent
review read the probe next to the middleware's allowed hosts and flagged it as blocking.
Reproduced locally with the same middleware and allowed hosts: `Host: 10.244.0.12:8081`
gets 400, `Host: localhost` gets 200.

**How we fixed it.** In #159, before it merged. The probe sends `Host: localhost`, which the
gateway allows, so it is still a real HTTP check. A unit test fails on any HTTP probe to a
server started with `--allowed-host` that sends no Host header, or one not on its list (it
reads the real arguments, and fails if it finds no such server to check).

**What stops it coming back.** The kind E2E now ends by failing if any container was
restarted by a liveness or startup probe during the run (the kubelet's `Killing` events), and
the check itself fails if it cannot read the events. The first green run printed `no container
restarted by a liveness or startup probe`. 13.3's bad release crashes at start instead of
failing a probe, so the drill and this check do not get in each other's way.
