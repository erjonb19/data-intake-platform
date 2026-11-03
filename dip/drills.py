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


def airflow(run: Callable[..., str], *args: str) -> str:
    """The Airflow CLI in the scheduler's pod (the API server would need a login)."""
    return run("-n", NS, "exec", "deploy/airflow-scheduler", "--", "airflow", *args)


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


DRILLS: dict[str, tuple[Callable[..., str], Callable[..., str]]] = {
    "scheduler_down": (scheduler_down, scheduler_down_undo),
    "oom_task": (oom_task, oom_task_undo),
    "bad_secret": (bad_secret, bad_secret_undo),
}


def drill(name: str, undo: bool = False, run: Callable[..., str] = kubectl) -> str:
    if name not in DRILLS:
        raise DrillError(f"unknown drill {name!r}: one of {sorted(DRILLS)}")
    require_kind(run)
    do, undo_fn = DRILLS[name]
    return (undo_fn if undo else do)(run)
