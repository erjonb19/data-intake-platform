"""`dip rollback SERVICE` (13.2): put a Deployment back on its previous revision.

    python -m dip rollback intake-api

`kubectl rollout undo` on the service's Deployment, then `kubectl rollout status` until the
old revision is Ready again. Like the drills, it refuses unless kubectl's current context is
the kind cluster `kind-dip` (dip/drills.py): never a real cluster.

The rolling strategy (`maxUnavailable: 0`) is what makes this safe: a release whose pod never
gets Ready stalls with the old pod still serving, and the undo only has to scale the stalled
ReplicaSet away. Terraform still owns the Deployment, so the next `terraform apply` puts the
image Terraform names back; fix the release there too (runbook 13, with 13.3).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from dip.drills import DrillError, kubectl, require_kind

# service name -> (namespace, Deployment). Only the stateless services Terraform rolls with
# `maxUnavailable: 0`; MinIO and SFTP use Recreate (their volumes are ReadWriteOnce), and
# Airflow's Deployments belong to its Helm release (`helm rollback`).
SERVICES: dict[str, tuple[str, str]] = {
    "intake-api": ("dip-data", "intake-api"),
    "mcp": ("dip-data", "mcp"),
    "approvals": ("dip-data", "approvals"),
    "metrics": ("dip-data", "dip-metrics"),
    "marquez": ("dip-obs", "marquez"),
    "marquez-web": ("dip-obs", "marquez-web"),
}


def rollback(service: str, run: Callable[..., str] = kubectl, timeout: str = "180s",
             sleep: Callable[[float], None] = time.sleep) -> str:  # fmt: skip
    if service not in SERVICES:
        raise DrillError(f"unknown service {service!r}; one of: {', '.join(sorted(SERVICES))}")
    require_kind(run)
    ns, deploy = SERVICES[service]
    before = run("-n", ns, "get", f"deployment/{deploy}",
                 "-o", "jsonpath={.spec.template.spec.containers[0].image}")  # fmt: skip
    run("-n", ns, "rollout", "undo", f"deployment/{deploy}")
    # Undoing a stalled rollout: for one sync after the undo the controller can still
    # report ProgressDeadlineExceeded at the new generation, and `rollout status` fails at
    # once on that. It clears on the next sync, so try again a few times (review of #162).
    for attempt in range(4):
        try:
            run("-n", ns, "rollout", "status", f"deployment/{deploy}", f"--timeout={timeout}")
            break
        except DrillError as exc:
            if "progress deadline" not in str(exc) or attempt == 3:
                raise
            sleep(3)
    after = run("-n", ns, "get", f"deployment/{deploy}",
                "-o", "jsonpath={.spec.template.spec.containers[0].image}")  # fmt: skip
    return f"{service}: rolled back from {before} to {after}, and Ready"
