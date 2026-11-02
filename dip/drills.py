"""Platform drills (Stage 11; PLAN.md section 8.13, design choice 3): break the platform
itself, on purpose, on the kind cluster only, and undo it.

    python -m dip drill NAME [--undo]

Each drill changes one thing in the cluster with kubectl and has an undo that restores it.
The command refuses to run unless kubectl's current context is the kind cluster `kind-dip`
(KUBECONFIG as up.sh writes it): never a real cluster. The drills run in the GitHub proof
workflow, never on the laptop (decisions log, 2026-10-30).

  scheduler_down   scale the Airflow scheduler Deployment to 0      (runbook 09, S11.6)
"""

from __future__ import annotations

import subprocess
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


DRILLS: dict[str, tuple[Callable[..., str], Callable[..., str]]] = {
    "scheduler_down": (scheduler_down, scheduler_down_undo),
}


def drill(name: str, undo: bool = False, run: Callable[..., str] = kubectl) -> str:
    if name not in DRILLS:
        raise DrillError(f"unknown drill {name!r}: one of {sorted(DRILLS)}")
    require_kind(run)
    do, undo_fn = DRILLS[name]
    return (undo_fn if undo else do)(run)
