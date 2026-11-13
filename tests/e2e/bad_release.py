"""13.3: the bad_release drill through the deployed stack, after the E2E's liveness check.

  1. the platform check sees no stalled rollout;
  2. `dip drill bad_release` ships an intake API release whose pod crashes at start and
     never gets Ready; the Deployment keeps one available pod the whole time (13.2's
     maxUnavailable 0: the old pod serves);
  3. past the 3-minute progress deadline Kubernetes marks the rollout stalled, and the
     platform check opens PLATFORM.ROLLOUT_STALLED on intake-api; with the observability
     profile, the ReleaseStalled alert fires;
  4. `dip drill bad_release --undo` (that is `dip rollback intake-api`) restores the old
     revision, Ready, and the check closes the incident.

Env: KUBECONFIG (context kind-dip), WAREHOUSE_DSN, PROMETHEUS_URL (empty without the
observability profile).
"""

from __future__ import annotations

import os
import sys
import threading
import time

from platform_drill import dip, firing, kubectl, open_incidents, run_check

KIND = "PLATFORM.ROLLOUT_STALLED"
DEPLOY = ("-n", "dip-data", "get", "deployment/intake-api", "-o")


def available() -> int:
    return int(kubectl(*DEPLOY, "jsonpath={.status.availableReplicas}") or 0)


def stalled() -> bool:
    conds = kubectl(*DEPLOY, "jsonpath={.status.conditions[?(@.type=='Progressing')].reason}")
    return conds == "ProgressDeadlineExceeded"


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    run_check("release-before")
    check(("intake-api",) not in open_incidents(KIND), "no stalled rollout before the drill")

    low, stop = [99], threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                low[0] = min(low[0], available())
            except Exception:  # noqa: BLE001 - an unreadable Deployment counts as down
                low[0] = 0
            time.sleep(2)

    watcher = threading.Thread(target=watch, daemon=True)
    started = time.monotonic()
    watcher.start()
    try:
        # inside the try: a drill that fails after its patch went through is still undone
        print(dip("drill", "bad_release"))
        while not stalled() and time.monotonic() - started < 300:  # deadline 180 s
            time.sleep(10)
        check(stalled(), "Kubernetes marked the rollout stalled (ProgressDeadlineExceeded)")
        opened = False
        while not opened and time.monotonic() - started < 420:
            run_check("release-stalled")
            opened = ("intake-api",) in open_incidents(KIND)
            if not opened:
                time.sleep(15)
        check(opened, f"the check opened {KIND} on intake-api")
        prom = os.environ.get("PROMETHEUS_URL", "")
        if prom:
            seen = False
            while not seen and time.monotonic() - started < 600:
                seen = firing(prom, "ReleaseStalled")
                if not seen:
                    time.sleep(15)
            check(seen, "the ReleaseStalled alert fires")
    finally:
        try:
            print(dip("drill", "bad_release", "--undo"))
        finally:
            stop.set()
            watcher.join(5)
    check(low[0] >= 1, f"the old pod served throughout (fewest available: {low[0]})")
    undone, closed = time.monotonic(), False
    while not closed and time.monotonic() - undone < 180:
        run_check("release-after")
        closed = ("intake-api",) not in open_incidents(KIND)
        if not closed:
            time.sleep(15)
    check(closed, "after the rollback the check closed the incident")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: the bad_release drill stalled with the old pod serving, opened its incident"
          " and alert, and dip rollback closed it")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
