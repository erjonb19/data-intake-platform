"""13.2: a rollout and `dip rollback` on the deployed intake API, without downtime.

  1. `kubectl rollout restart` makes a new revision of the intake-api Deployment (the same
     image, a new pod template annotation);
  2. while it rolls, the Deployment never has fewer than one available pod
     (`maxUnavailable: 0`: the new pod is Ready before the old one goes);
  3. `dip rollback intake-api` puts the previous revision back (the annotation is gone)
     and waits until it is Ready, again never dropping to zero available pods.

The bad release that never gets Ready, its alert and runbook are 13.3's drill.

Env: KUBECONFIG (the kind cluster's, context kind-dip).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

NS, DEPLOY = "dip-data", "deployment/intake-api"
REPO = Path(__file__).resolve().parents[2]
RESTARTED = "kubectl.kubernetes.io/restartedAt"


def kubectl(*args: str) -> str:
    out = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                         timeout=300)  # fmt: skip
    if out.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def available() -> int:
    return int(kubectl("get", DEPLOY, "-o", "jsonpath={.status.availableReplicas}") or 0)


class Watch(threading.Thread):
    """Samples availableReplicas every second; `low` is the fewest seen."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.low, self.stop = 99, threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.low = min(self.low, available())
            except (RuntimeError, ValueError):
                self.low = 0  # an unreadable Deployment counts as down
            time.sleep(1)


def watched(step) -> int:
    w = Watch()
    w.start()
    try:
        step()
    finally:
        w.stop.set()
        w.join(5)
    return w.low


def annotations() -> str:
    return kubectl("get", DEPLOY, "-o", "jsonpath={.spec.template.metadata.annotations}")


def main() -> int:
    assert available() >= 1, "intake-api is not available before the rollout"
    assert RESTARTED not in annotations(), "intake-api was restarted before this check"

    low = watched(lambda: (kubectl("rollout", "restart", DEPLOY),
                           kubectl("rollout", "status", DEPLOY, "--timeout=180s")))  # fmt: skip
    assert low >= 1, f"the rollout dropped intake-api to {low} available pods"
    assert RESTARTED in annotations()
    print(f"rollout: new revision Ready, never below {low} available pod(s)")

    def undo() -> None:
        out = subprocess.run([sys.executable, "-m", "dip", "rollback", "intake-api"],
                             capture_output=True, text=True, cwd=REPO, timeout=300)  # fmt: skip
        print(out.stdout.strip() or out.stderr.strip())
        assert out.returncode == 0, f"dip rollback failed: {out.stderr.strip()[-300:]}"

    low = watched(undo)
    assert low >= 1, f"the rollback dropped intake-api to {low} available pods"
    assert RESTARTED not in annotations(), "the rollback did not restore the previous template"
    print(f"rollback: previous revision Ready, never below {low} available pod(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
