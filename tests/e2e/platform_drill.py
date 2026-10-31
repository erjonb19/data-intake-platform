"""Stage 11 (S11.6): the scheduler_down drill through the deployed stack, last in the E2E
(it stops scheduling for a few minutes).

  1. the platform check (the CronJob, run now as a Job) sees a healthy scheduler;
  2. `dip drill scheduler_down` scales the scheduler to 0; once its heartbeat is older than
     the check's 90 s, the check opens PLATFORM.SCHEDULER_DOWN (target scheduler);
  3. with the observability profile, the AirflowSchedulerDown alert fires (> 180 s);
  4. `dip drill scheduler_down --undo` brings it back, and the check closes the incident.

The check is run on demand (kubectl create job --from=cronjob/platform-check) instead of
waiting for its 2-minute schedule, so the drill takes about 5 minutes, not 10.

Env: KUBECONFIG (the kind cluster's, context kind-dip), WAREHOUSE_DSN, PROMETHEUS_URL
(empty without the observability profile).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

NS = "dip-airflow"
REPO = Path(__file__).resolve().parents[2]


def kubectl(*args: str) -> str:
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def run_check(tag: str) -> None:
    """One run of the platform check, now, and wait for it to finish."""
    name = f"platform-check-e2e-{tag}-{int(time.time()) % 100000}"
    kubectl("-n", NS, "create", "job", name, "--from=cronjob/platform-check")
    kubectl("-n", NS, "wait", "--for=condition=complete", f"job/{name}", "--timeout=120s")
    print(kubectl("-n", NS, "logs", f"job/{name}")[-300:])


def scheduler_incidents() -> list[tuple]:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select feed, target, closed_at is null from ops.incident"
                    " where kind = 'PLATFORM.SCHEDULER_DOWN' order by incident_id")  # fmt: skip
        return cur.fetchall()


def firing(prom: str, alert: str) -> bool:
    q = urllib.parse.urlencode({"query": f'ALERTS{{alertname="{alert}",alertstate="firing"}}'})
    with urllib.request.urlopen(f"{prom}/api/v1/query?{q}", timeout=10) as r:
        return bool(json.loads(r.read())["data"]["result"])


def dip(*args: str) -> str:
    # from the repository root, as the break loop runs it (tests/e2e/break_loop.py)
    out = subprocess.run([sys.executable, "-m", "dip", *args], capture_output=True, text=True,
                         timeout=300, cwd=REPO, env={**os.environ, "PYTHONUTF8": "1"})  # fmt: skip
    if out.returncode:
        raise RuntimeError(f"dip {' '.join(args)}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"==> {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            failures.append(what)

    run_check("before")
    check(not any(open_ for _, _, open_ in scheduler_incidents()),
          "a healthy scheduler: no open PLATFORM.SCHEDULER_DOWN")  # fmt: skip
    print(dip("drill", "scheduler_down"))
    started = time.monotonic()
    try:
        # The heartbeat goes stale after 90 s: run the check until the incident opens (the
        # spec: within 4 minutes), not once after a fixed wait.
        time.sleep(90)
        opened, got = False, []
        while not opened and time.monotonic() - started < 240:
            run_check("down")
            got = scheduler_incidents()
            opened = any(f == "_platform" and t == "scheduler" and o for f, t, o in got)
            if not opened:
                time.sleep(15)
        check(opened, f"the drill opened PLATFORM.SCHEDULER_DOWN on the scheduler: {got}")
        prom = os.environ.get("PROMETHEUS_URL", "")
        if prom:
            seen = False
            while not seen and time.monotonic() - started < 420:
                seen = firing(prom, "AirflowSchedulerDown")
                if not seen:
                    time.sleep(15)
            check(seen, "the AirflowSchedulerDown alert fires")
    finally:
        print(dip("drill", "scheduler_down", "--undo"))
    undone, closed = time.monotonic(), False
    while not closed and time.monotonic() - undone < 180:  # a heartbeat or two
        time.sleep(15)
        run_check("after")
        closed = not any(open_ for _, _, open_ in scheduler_incidents())
    check(closed, "after --undo the check closed the incident")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: the scheduler_down drill opened and closed its incident")
    return 0


if __name__ == "__main__":
    sys.exit(main())
