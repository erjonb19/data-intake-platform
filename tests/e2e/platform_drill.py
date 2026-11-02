"""Stage 11 (S11.6): the scheduler_down drill through the deployed stack, last in the E2E
(it stops scheduling for a few minutes); then oom_task (S11.7), bad_secret (S11.8) and
stuck_queued (S11.9).

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


def firing(prom: str, alert: str, dag_id: str = "") -> bool:
    dag = f',dag_id="{dag_id}"' if dag_id else ""
    q = urllib.parse.urlencode(
        {"query": f'ALERTS{{alertname="{alert}",alertstate="firing"{dag}}}'})  # fmt: skip
    with urllib.request.urlopen(f"{prom}/api/v1/query?{q}", timeout=10) as r:
        return bool(json.loads(r.read())["data"]["result"])


def dip(*args: str) -> str:
    # from the repository root, as the break loop runs it (tests/e2e/break_loop.py)
    out = subprocess.run([sys.executable, "-m", "dip", *args], capture_output=True, text=True,
                         timeout=300, cwd=REPO, env={**os.environ, "PYTHONUTF8": "1"})  # fmt: skip
    if out.returncode:
        raise RuntimeError(f"dip {' '.join(args)}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def open_incidents(kind: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select target from ops.incident where kind = %s and closed_at is null",
                    (kind,))  # fmt: skip
        return cur.fetchall()


def oom_drill(check) -> None:
    """Stage 11 (S11.7): drill_oom's task is OOMKilled; the check opens TASK.OOMKILLED for
    drill_oom.eat_memory, TaskPodOOMKilled fires, and after --undo the check closes it."""
    started, opened = time.monotonic(), False
    try:  # the undo runs even when the drill itself fails half-way
        print(dip("drill", "oom_task"))
        while not opened and time.monotonic() - started < 300:  # the run starts, the pod dies
            time.sleep(20)
            run_check("oom")
            opened = ("drill_oom.eat_memory",) in open_incidents("TASK.OOMKILLED")
        check(opened, f"the drill opened TASK.OOMKILLED: {open_incidents('TASK.OOMKILLED')}")
        prom = os.environ.get("PROMETHEUS_URL", "")
        if prom and opened:
            seen = False
            while not seen and time.monotonic() - started < 420:
                seen = firing(prom, "TaskPodOOMKilled")
                if not seen:
                    time.sleep(15)
            check(seen, "the TaskPodOOMKilled alert fires")
    finally:
        print(dip("drill", "oom_task", "--undo"))
    undone, closed = time.monotonic(), False
    while not closed and time.monotonic() - undone < 180:
        time.sleep(15)
        # a task queued before the pause can still start a pod after the first delete
        dip("drill", "oom_task", "--undo")
        run_check("oom-after")
        closed = not open_incidents("TASK.OOMKILLED")
    check(closed, "after --undo the check closed TASK.OOMKILLED")


FEED_DAG = "intake__healthcare__eligibility"


def warehouse_secret() -> tuple[str, str]:
    """The password airflow-connections holds now, and the saved real one ("" if none)."""
    import base64

    def pw(name: str) -> str:
        out = kubectl("-n", NS, "get", "secret", name, "-o", "jsonpath={.data.warehouse}",
                      "--ignore-not-found")  # fmt: skip
        uri = base64.b64decode(out).decode() if out else ""
        return urllib.parse.urlsplit(uri).password or "" if uri else ""

    return pw("airflow-connections"), pw("dip-drill-saved-warehouse")


def intake_log() -> str:
    """The failed intake task's log of the feed DAG's latest run, as Airflow serves it."""
    import urllib.request

    url = os.environ.get("AIRFLOW_URL", "http://localhost:8080").rstrip("/")

    def call(method: str, path: str, body: dict | None = None, token: str = "") -> dict:
        req = urllib.request.Request(url + path, method=method,
                                     data=json.dumps(body).encode() if body else None)  # fmt: skip
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    token = call("POST", "/auth/token", {"username": os.environ.get("AIRFLOW_USER", "admin"),
                                         "password": os.environ["AIRFLOW_PASSWORD"]}
                 )["access_token"]  # fmt: skip
    api = f"/api/v2/dags/{FEED_DAG}/dagRuns"
    run = call("GET", f"{api}?order_by=-run_after&limit=5", token=token)["dag_runs"]
    failed = next(r for r in run if r["state"] == "failed")
    rid = urllib.parse.quote(failed["dag_run_id"], safe=":+")
    ti = call("GET", f"{api}/{rid}/taskInstances/intake", token=token)
    got = call("GET", f"{api}/{rid}/taskInstances/intake/logs/{ti.get('try_number') or 1}",
               token=token)  # fmt: skip
    content = got.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(c.get("event", c)) if isinstance(c, dict) else str(c)
                            for c in content)  # fmt: skip
    return str(content)


def logs_with(*needles: str) -> list[str]:
    """The pods in dip-airflow whose logs hold any of the strings."""
    hits = []
    for pod in kubectl("-n", NS, "get", "pods", "-o", "name").split():
        cmd = ["kubectl", "-n", NS, "logs", pod, "--all-containers", "--tail=5000"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if any(n and n in out.stdout for n in needles):
            hits.append(pod)
    return hits


def dag_failed_open() -> list[tuple]:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute("select target from ops.incident where kind = 'DAG.FAILED'"
                    " and feed = 'healthcare.eligibility' and closed_at is null")  # fmt: skip
        open_ = cur.fetchall()
        cur.execute("select count(*) from ops.platform_check where check_name = 'dag_run'"
                    " and target = %s and not ok", (FEED_DAG,))  # fmt: skip
        return open_ + [("seen by the check",)] * min(1, cur.fetchone()[0])


def bad_secret_drill(check) -> None:
    """Stage 11 (S11.8): a wrong warehouse password in airflow-connections. The feed DAG's
    intake task cannot connect; the platform check (its own login) sees the failed run and
    DAG.FAILED opens (target the DAG); DagRunFailing fires; no pod's log holds either
    password; after --undo the next run succeeds and DAG.FAILED closes."""
    started, opened = time.monotonic(), False
    try:  # the undo runs even when the drill itself fails half-way
        print(dip("drill", "bad_secret"))
        bad, real = warehouse_secret()
        check(bool(bad) and bool(real) and bad != real, "the drill swapped the password")
        while not opened and time.monotonic() - started < 300:
            time.sleep(20)
            run_check("secret")
            got = dag_failed_open()
            opened = (FEED_DAG,) in got and ("seen by the check",) in got
        check(opened, f"the check saw the failed run; DAG.FAILED is open: {dag_failed_open()}")
        prom = os.environ.get("PROMETHEUS_URL", "")
        if prom and opened:
            seen = False
            while not seen and time.monotonic() - started < 600:
                seen = firing(prom, "DagRunFailing", FEED_DAG)  # not drill_oom's
                if not seen:
                    time.sleep(15)
            check(seen, f"the DagRunFailing alert fires for {FEED_DAG}")
        log = intake_log()
        check('password authentication failed for user "warehouse"' in log,
              "the intake task's log shows the refused login")  # fmt: skip
        check(bad not in log and real not in log, "the task log holds neither password")
        check(not logs_with(bad, real), "no pod's log holds the wrong or the real password")
    finally:
        print(dip("drill", "bad_secret", "--undo"))
    undone, closed = time.monotonic(), False
    while not closed and time.monotonic() - undone < 420:  # the DAG's next run succeeds
        time.sleep(20)
        run_check("secret-after")
        closed = (FEED_DAG,) not in dag_failed_open()
    check(closed, "after --undo the feed's next run closed DAG.FAILED")


def stuck_threshold() -> str:
    path = ('jsonpath={.spec.jobTemplate.spec.template.spec.containers[0]'
            '.env[?(@.name=="DIP_QUEUED_STALE_SECONDS")].value}')  # fmt: skip
    return kubectl("-n", NS, "get", "cronjob/platform-check", "-o", path)


def stuck_queued_drill(check) -> None:
    """Stage 11 (S11.9): drill_queue's pod can never be scheduled, so its task stays queued;
    with the threshold lowered to 120 s the check opens TASK.STUCK_QUEUED for
    drill_queue.wait_for_room (before Airflow's 600 s task_queued_timeout), the pod's events
    say FailedScheduling, TaskQueuedTooLong fires, and after --undo the check closes it and
    the threshold is back."""
    target = ("drill_queue.wait_for_room",)
    before = stuck_threshold()
    started, opened = time.monotonic(), False
    try:  # the undo runs even when the drill itself fails half-way
        print(dip("drill", "stuck_queued"))
        check(stuck_threshold() == "120", "the drill lowered the check's threshold to 120 s")
        time.sleep(120)
        while not opened and time.monotonic() - started < 360:
            run_check("queue")
            opened = target in open_incidents("TASK.STUCK_QUEUED")
            if not opened:
                time.sleep(20)
        check(opened, f"the drill opened TASK.STUCK_QUEUED: {open_incidents('TASK.STUCK_QUEUED')}")
        names = "jsonpath={.items[*].involvedObject.name}"
        events = kubectl("-n", NS, "get", "events", "--field-selector",
                         "reason=FailedScheduling", "-o", names)  # fmt: skip
        check("drill-queue" in events, f"the drill's pod is FailedScheduling: {events[-200:]}")
        prom = os.environ.get("PROMETHEUS_URL", "")
        if prom and opened:
            seen = False
            while not seen and time.monotonic() - started < 540:
                seen = firing(prom, "TaskQueuedTooLong")
                if not seen:
                    time.sleep(15)
            check(seen, "the TaskQueuedTooLong alert fires")
    finally:
        print(dip("drill", "stuck_queued", "--undo"))
    check(stuck_threshold() == (before or "300"), "--undo restored the check's threshold")
    undone, closed = time.monotonic(), False
    while not closed and time.monotonic() - undone < 240:
        time.sleep(15)
        run_check("queue-after")
        closed = target not in open_incidents("TASK.STUCK_QUEUED")
    check(closed, "after --undo the check closed TASK.STUCK_QUEUED")


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
    oom_drill(check)
    bad_secret_drill(check)
    stuck_queued_drill(check)
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: the scheduler_down, oom_task, bad_secret and stuck_queued drills opened and"
          " closed their incidents")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
