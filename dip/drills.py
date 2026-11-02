"""Platform drills (Stage 11; PLAN.md section 8.13, design choice 3): break the platform
itself, on purpose, on the kind cluster only, and undo it.

    python -m dip drill NAME [--undo]

Each drill changes one thing in the cluster with kubectl and has an undo that restores it.
The command refuses to run unless kubectl's current context is the kind cluster `kind-dip`
(KUBECONFIG as up.sh writes it): never a real cluster. The drills run in the GitHub proof
workflow, never on the laptop (decisions log, 2026-10-30).

  scheduler_down   scale the Airflow scheduler Deployment to 0      (runbook 09, S11.6)
  oom_task         run the drill_oom DAG, whose task is OOMKilled   (runbook 10, S11.7)
  bad_secret       a wrong warehouse password in airflow-connections, then a run of
                   the eligibility feed's DAG, whose intake task cannot connect
                                                                    (runbook 11, S11.8)
  stuck_queued     run the drill_queue DAG, whose task's pod can never be scheduled, with
                   the platform check's threshold lowered to 2 minutes
                                                                    (runbook 12, S11.9)
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable

NS = "dip-airflow"
CONTEXT = "kind-dip"


class DrillError(RuntimeError):
    pass


def kubectl(*args: str, check: bool = True) -> str:
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if check and out.returncode:
        raise DrillError(f"kubectl {' '.join(args[:3])}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def require_kind(run: Callable[..., str] = kubectl) -> None:
    ctx = run("config", "current-context", check=False)
    if ctx != CONTEXT:
        raise DrillError(f"refusing to drill: the kube context is {ctx or 'unset'!r},"
                         f" not the kind cluster {CONTEXT!r}")  # fmt: skip


def scheduler_down(run: Callable[..., str] = kubectl) -> str:
    run("-n", NS, "scale", "deployment", "airflow-scheduler", "--replicas=0")
    return "the scheduler is scaled to 0: within about 4 minutes PLATFORM.SCHEDULER_DOWN opens"


def scheduler_down_undo(run: Callable[..., str] = kubectl) -> str:
    run("-n", NS, "scale", "deployment", "airflow-scheduler", "--replicas=1")
    run("-n", NS, "rollout", "status", "deployment/airflow-scheduler", "--timeout=180s")
    return "the scheduler is back: the next platform check closes the incident"


def airflow(run: Callable[..., str], *args: str, check: bool = True) -> str:
    """The Airflow CLI in the scheduler's pod (the API server would need a login)."""
    return run("-n", NS, "exec", "deploy/airflow-scheduler", "--", "airflow", *args,
               check=check)  # fmt: skip


def oom_task(run: Callable[..., str] = kubectl) -> str:
    airflow(run, "dags", "unpause", "drill_oom")
    airflow(run, "dags", "trigger", "drill_oom")
    return "drill_oom runs: its task is OOMKilled, and the next check opens TASK.OOMKILLED"


def oom_task_undo(run: Callable[..., str] = kubectl) -> str:
    airflow(run, "dags", "pause", "drill_oom")
    run("-n", NS, "delete", "pod", "-l", "dag_id=drill_oom", "--ignore-not-found",
        "--wait=false")  # fmt: skip
    return "drill_oom is paused and its pods are gone: the next check closes the incident"


# bad_secret: the Secret Airflow's pods read AIRFLOW_CONN_WAREHOUSE from (Terraform writes
# it), the Secret that keeps the real value while the drill is on (a Secret, not an
# annotation: `kubectl describe` prints annotations), and the feed DAG it runs.
SECRET = "airflow-connections"
SAVED = "dip-drill-saved-warehouse"
FEED_DAG = "intake__healthcare__eligibility"


def _get_secret(run: Callable[..., str], name: str) -> dict | None:
    out = run("-n", NS, "get", "secret", name, "-o", "json", "--ignore-not-found")
    return json.loads(out) if out else None


def _from_file(run: Callable[..., str], doc: dict, *args: str) -> None:
    """kubectl with the document in a file, never in its arguments (another process on the
    machine can read those), the file removed after."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        run(*[path if a == "{file}" else a for a in args])
    finally:
        os.unlink(path)


def _set_warehouse(run: Callable[..., str], b64: str) -> None:
    patch = ("-n", NS, "patch", "secret", SECRET, "--type=merge", "--patch-file", "{file}")
    _from_file(run, {"data": {"warehouse": b64}}, *patch)


def wrong_password(uri: str) -> str:
    """The same connection with a wrong password: the "rotated in Postgres, not in the
    Secret" case. Random each time, so no log can match a fixed string by chance."""
    u = urllib.parse.urlsplit(uri)
    port = f":{u.port}" if u.port else ""
    return u._replace(netloc=f"{u.username}:drill-{secrets.token_hex(8)}@{u.hostname}{port}"
                      ).geturl()  # fmt: skip


def bad_secret(run: Callable[..., str] = kubectl) -> str:
    if _get_secret(run, SAVED) is not None:
        raise DrillError("bad_secret is already on: run it with --undo first")
    good = _get_secret(run, SECRET)["data"]["warehouse"]  # base64, as held; never printed
    bad = wrong_password(base64.b64decode(good).decode("utf-8"))
    _from_file(run, {"apiVersion": "v1", "kind": "Secret",
                     "metadata": {"name": SAVED, "namespace": NS},
                     "data": {"warehouse": good}}, "apply", "-f", "{file}")  # fmt: skip
    _set_warehouse(run, base64.b64encode(bad.encode()).decode())
    # Pods read the Secret when they start: the running scheduler, DAG processor and API
    # server keep the real password; the next task pod gets the wrong one.
    airflow(run, "dags", "unpause", FEED_DAG)
    airflow(run, "dags", "trigger", FEED_DAG)
    return (f"airflow-connections holds a wrong warehouse password and {FEED_DAG} runs: its"
            " intake task fails to connect, and DAG.FAILED opens")  # fmt: skip


def bad_secret_undo(run: Callable[..., str] = kubectl) -> str:
    saved = _get_secret(run, SAVED)
    if saved is None:
        return "bad_secret is not on: nothing to restore"
    _set_warehouse(run, saved["data"]["warehouse"])
    run("-n", NS, "delete", "secret", SAVED)
    run("-n", NS, "delete", "pod", "-l", f"dag_id={FEED_DAG}",
        "--field-selector=status.phase=Failed", "--ignore-not-found", "--wait=false")  # fmt: skip
    airflow(run, "dags", "trigger", FEED_DAG)
    return (f"the real password is back and {FEED_DAG} runs again: its success closes"
            " DAG.FAILED")  # fmt: skip


# stuck_queued: the platform check's threshold for a waiting task, lowered for the drill on
# the CronJob (Terraform sets it), and the CronJob annotation that keeps the value it had.
CHECK = "cronjob/platform-check"
STALE_ENV = "DIP_QUEUED_STALE_SECONDS"
STALE_SAVED = "dip.drill/queued-stale-seconds"
DRILL_STALE = "120"


def _saved_threshold(run: Callable[..., str]) -> str | None:
    annotations = json.loads(run("-n", NS, "get", CHECK, "-o",
                                 "jsonpath={.metadata.annotations}") or "{}")  # fmt: skip
    return annotations.get(STALE_SAVED)


def stuck_queued(run: Callable[..., str] = kubectl) -> str:
    if _saved_threshold(run) is not None:
        raise DrillError("stuck_queued is already on: run it with --undo first")
    # The DAG first: if drill_queue is not parsed yet (a fresh deploy, or just after an
    # undo deleted it), this fails before the check's threshold is touched.
    airflow(run, "dags", "unpause", "drill_queue")
    airflow(run, "dags", "trigger", "drill_queue")
    was = run("-n", NS, "get", CHECK, "-o", "jsonpath={.spec.jobTemplate.spec.template.spec"
              f".containers[0].env[?(@.name==\"{STALE_ENV}\")].value}}")  # fmt: skip
    run("-n", NS, "annotate", CHECK, f"{STALE_SAVED}={was or '300'}", "--overwrite")
    run("-n", NS, "set", "env", CHECK, f"{STALE_ENV}={DRILL_STALE}")
    return ("drill_queue runs: its pod stays Pending and its task queued; after"
            f" {DRILL_STALE} s the next check opens TASK.STUCK_QUEUED")  # fmt: skip


def stuck_queued_undo(run: Callable[..., str] = kubectl) -> str:
    # The check's threshold first: it depends on nothing below, and a slow scheduler
    # restart must not leave every later check at the drill's 120 s.
    saved = _saved_threshold(run)
    if saved is not None:
        run("-n", NS, "set", "env", CHECK, f"{STALE_ENV}={saved}")
        run("-n", NS, "annotate", CHECK, f"{STALE_SAVED}-")
    # Safe to run twice: between `dags delete` and the DAG processor's next parse there is
    # no drill_queue to pause or delete (and nothing left to undo there).
    airflow(run, "dags", "pause", "drill_queue", check=False)
    run("-n", NS, "delete", "pod", "-l", "dag_id=drill_queue", "--ignore-not-found",
        "--wait=false")  # fmt: skip
    # A Pending pod's deletion is not reported to Airflow (the KubernetesExecutor only logs
    # it), so the task would stay queued, holding one of the executor's slots, until
    # task_queued_timeout. Instead: delete the drill DAG's runs (the DAG processor adds the
    # DAG back, paused), and restart the scheduler, whose executor then forgets the slot.
    airflow(run, "dags", "delete", "drill_queue", "--yes", check=False)
    left = airflow(run, "dags", "list-runs", "drill_queue", "-o", "json", check=False)
    if left.startswith("[") and json.loads(left):  # the DAG is there and still has runs
        raise DrillError("drill_queue's runs were not deleted: run --undo again")
    run("-n", NS, "rollout", "restart", "deployment/airflow-scheduler")
    run("-n", NS, "rollout", "status", "deployment/airflow-scheduler", "--timeout=180s")
    return ("drill_queue is paused, its run and pod are gone and the scheduler restarted:"
            " the next check closes TASK.STUCK_QUEUED")  # fmt: skip


DRILLS: dict[str, tuple[Callable[..., str], Callable[..., str]]] = {
    "scheduler_down": (scheduler_down, scheduler_down_undo),
    "oom_task": (oom_task, oom_task_undo),
    "bad_secret": (bad_secret, bad_secret_undo),
    "stuck_queued": (stuck_queued, stuck_queued_undo),
}


def drill(name: str, undo: bool = False, run: Callable[..., str] = kubectl) -> str:
    if name not in DRILLS:
        raise DrillError(f"unknown drill {name!r}: one of {sorted(DRILLS)}")
    require_kind(run)
    do, undo_fn = DRILLS[name]
    return (undo_fn if undo else do)(run)
