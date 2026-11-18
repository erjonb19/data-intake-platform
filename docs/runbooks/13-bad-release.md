# Runbook 13: a release that never gets ready

**Signal.** An open `PLATFORM.ROLLOUT_STALLED` incident in `ops.incident` (feed `_platform`,
target the Deployment's name, such as `intake-api`), opened by the `platform-check` CronJob
when a Deployment in `dip-data` passed its progress deadline: Kubernetes set its
`Progressing` condition to `False` with reason `ProgressDeadlineExceeded`. The incident's
detail quotes that message (`ReplicaSet "<name>" has timed out progressing`). Alert
**ReleaseStalled**.

The rolling services, `intake-api`, `mcp`, `approvals` and `dip-metrics`, have a 180 s
deadline and `maxUnavailable: 0`: the old pod is not removed until a new one is Ready, so the
old version is **still serving**. Nothing is down yet, but the change did not ship.
`minio` and `sftp` are different: they use `Recreate` (their volumes are ReadWriteOnce) and
Kubernetes' default 600 s deadline, so their old pod is already gone and the service **is
down** until the new pod is Ready.

**Break it on purpose (kind only):** `python -m dip drill bad_release` changes the intake
API's command to an app that does not exist (`dip.intake_api:no_such_app`), so the new pod
crashes at start; `python -m dip rollback intake-api` undoes it. The kind E2E runs it last.

## Evidence

1. `get_incident(incident_id)`: which Deployment (the target) and the progress-deadline
   message in the detail.
2. What changed: the Deployment's last rollout, by hand,
   `kubectl -n dip-data rollout history deployment/<name>`, and the new pod's state,
   `kubectl -n dip-data get pods -l app=<name>` (`CrashLoopBackOff`, `ImagePullBackOff`,
   `ErrImageNeverPull`, or `Running` but never `Ready`).
3. Why the new pod fails: `kubectl -n dip-data logs <new pod> --previous` (a crash at
   start: a wrong command, module or setting) or `kubectl -n dip-data describe pod <new pod>`
   (an image that is not there, a readiness probe that fails, a missing Secret key).

## Decide

A Deployment's new version never became ready: what to do, by which Deployment it is and
what the new pod shows.

| What you see | Action |
|---|---|
| A rolling service stalled (`intake-api`, `mcp`, `approvals`, `dip-metrics`), first | Roll back: `python -m dip rollback <service>` (`kubectl rollout undo`, kind-dip only), which brings back the previous ReplicaSet; the old pod kept serving, so nothing is lost. The service name is the Deployment's, except `dip-metrics`, which is `metrics` |
| `minio` or `sftp` stalled (Recreate: the service is down) | `dip rollback` refuses them; fix the change in Terraform and apply again, or put the previous version back by hand: `kubectl -n dip-data rollout undo deployment/<name>` against `kind-dip` only |
| The new pod crashes at start (`CrashLoopBackOff`, a traceback or "not found" in its previous log) | Fix the command, setting or code in the change, rebuild the image and apply again with Terraform |
| `ImagePullBackOff` or `ErrImageNeverPull`: the image tag is not in the cluster | Load the image into kind (`scripts/up.sh` builds and loads them) or fix the tag, then apply again |
| `Running` but never `Ready`: its readiness probe fails | Check the probe's path, port and `Host` header against the new version, then apply again |
| The drill (`dip.intake_api:no_such_app`) | Expected: `python -m dip rollback intake-api` |

## Check

`kubectl -n dip-data rollout status deployment/<name>` reports it successfully rolled out;
the next `platform-check` run (within 2 minutes) sees the Deployment progressing again and
closes the incident; **ReleaseStalled** clears. Terraform still owns the Deployment: the
next `terraform apply` sets it to what the code says, so a rollback without a fixed change
lasts until then.
