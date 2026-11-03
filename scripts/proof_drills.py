"""Stage 11 (S11.10): the platform drills with the incident agent live. Run by the proof
workflow's `drills` job after up.sh (core and observability profiles) and smoke.sh; the
stack runs without a model key until here.

    python scripts/proof_drills.py OUT_DIR

For each drill in turn (scheduler_down, oom_task, bad_secret, stuck_queued):

1. `dip drill NAME`; the platform check (its CronJob, run now as a Job) until the drill's
   incident is open.
2. The drill's alert seen in Prometheus.
3. The incident chain once, in the cluster, on that incident only: a pod of the platform
   image runs `python -m dip.agents.chain_job --limit 1 --incident ID` in the agent venv
   with the agent_chain DAG's Secret plus the model key (a Secret made for the run and
   deleted after) and DIP_SPEND_CAP_USD (the run's cap shared out across the drills).
   During bad_secret the pod reaches the warehouse with the password the drill saved
   (the airflow-connections Secret holds the wrong one by design).
4. The diagnosis's root cause against the expected one; the chain's model calls and cost
   (ops.agent_run_cost, by its chain id).
5. `dip drill NAME --undo`; the check until the incident closes.

OUT_DIR/results.md has one row per drill, and OUT_DIR/results.json the same. Exit 1 if any
check failed. Env: WAREHOUSE_DSN, KUBECONFIG, AIRFLOW_IMAGE (dip-airflow:<tag>),
ANTHROPIC_API_KEY, DIP_SPEND_CAP_USD (for the whole run, default 0.15), PROMETHEUS_URL,
AIRFLOW_URL and AIRFLOW_PASSWORD (as the E2E's drills use them). Every input is synthetic.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "e2e"))
sys.path.insert(0, str(REPO))

import platform_drill as pd  # noqa: E402  (the E2E's drill helpers)

NS = pd.NS
FEED_DAG = pd.FEED_DAG
DRILLS = [
    # name, incident kind, target, alert (name, dag label), expected root cause,
    # seconds before the first check, seconds to wait for the incident
    ("scheduler_down", "PLATFORM.SCHEDULER_DOWN", "scheduler", ("AirflowSchedulerDown", ""),
     "platform_scheduler", 90, 240),
    ("oom_task", "TASK.OOMKILLED", "drill_oom.eat_memory", ("TaskPodOOMKilled", ""),
     "platform_resources", 20, 300),
    ("bad_secret", "DAG.FAILED", FEED_DAG, ("DagRunFailing", FEED_DAG),
     "platform_credentials", 20, 300),
    ("stuck_queued", "TASK.STUCK_QUEUED", "drill_queue.wait_for_room",
     ("TaskQueuedTooLong", ""), "platform_capacity", 120, 360),
]  # fmt: skip


def job_tag(drill: str) -> str:
    """The drill's part of a Kubernetes Job or pod name: no underscores (DNS-1123)."""
    return drill.replace("_", "-")


def q(sql: str, *params) -> list[tuple]:
    import psycopg

    with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def open_incident_id(kind: str, target: str) -> int | None:
    got = q("select incident_id from ops.incident where kind = %s and target = %s"
            " and closed_at is null order by incident_id desc limit 1", kind, target)  # fmt: skip
    return got[0][0] if got else None


def wait_for(what, timeout: float, every: float = 20, tag: str = "proof"):
    """Run the platform check, then `what()`, until it is truthy or the time is up."""
    end = time.monotonic() + timeout
    while True:
        pd.run_check(tag)
        got = what()
        if got or time.monotonic() > end:
            return got
        time.sleep(every)


def alert_seen(alert: str, dag: str, timeout: float) -> bool:
    prom = os.environ.get("PROMETHEUS_URL", "")
    if not prom:
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pd.firing(prom, alert, dag):
            return True
        time.sleep(15)
    return False


def run_chain(incident_id: int, drill: str, cap: str) -> dict:
    """The chain once, in the cluster, on one incident; returns the pod's summary."""
    pod = f"chain-proof-{job_tag(drill)}"
    dsn_secret = "dip-drill-saved-warehouse" if drill == "bad_secret" else "airflow-connections"
    container = {
        "name": pod,
        "image": os.environ["AIRFLOW_IMAGE"],
        "command": [
            "/opt/airflow/agent-venv/bin/python",
            "-m",
            "dip.agents.chain_job",
            "--limit",
            "1",
            "--incident",
            str(incident_id),
            "--dag-run-id",
            f"s11.10-proof-{drill}",
        ],  # fmt: skip
        # The later source wins: the run's key over the DAG Secret's empty one.
        "envFrom": [
            {"secretRef": {"name": "dip-agent-chain"}},
            {"secretRef": {"name": "dip-proof-model"}},
        ],  # fmt: skip
        "env": [
            {"name": "DIP_SPEND_CAP_USD", "value": cap},
            {
                "name": "DIP_WAREHOUSE_DSN",
                "valueFrom": {"secretKeyRef": {"name": dsn_secret, "key": "warehouse"}},
            },
        ],  # fmt: skip
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },  # fmt: skip
    }
    overrides = {"apiVersion": "v1", "spec": {"containers": [container]}}
    try:
        image = f"--image={os.environ['AIRFLOW_IMAGE']}"
        pd.kubectl("-n", NS, "run", pod, "--restart=Never", image,
                   f"--overrides={json.dumps(overrides)}")  # fmt: skip
        phase = ""
        for _ in range(120):  # 10 minutes
            phase = pd.kubectl("-n", NS, "get", "pod", pod, "-o", "jsonpath={.status.phase}")
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(5)
        try:
            logs = pd.kubectl("-n", NS, "logs", pod)
        except RuntimeError as exc:
            logs = str(exc)
        print(logs[-3000:])
        lines = [ln for ln in logs.splitlines() if ln.startswith("{")]
        summary = json.loads(lines[-1]) if lines else {}
        summary["pod_phase"] = phase
        return summary
    finally:
        with contextlib.suppress(RuntimeError):
            pd.kubectl("-n", NS, "delete", "pod", pod, "--ignore-not-found", "--wait=false")


def model_key_secret(create: bool) -> None:
    if not create:
        with contextlib.suppress(RuntimeError):
            pd.kubectl("-n", NS, "delete", "secret", "dip-proof-model", "--ignore-not-found")
        return
    with tempfile.NamedTemporaryFile("w", delete=False) as f:  # never in an argument
        f.write(os.environ["ANTHROPIC_API_KEY"])
    try:
        pd.kubectl("-n", NS, "create", "secret", "generic", "dip-proof-model",
                   f"--from-file=ANTHROPIC_API_KEY={f.name}")  # fmt: skip
    finally:
        os.unlink(f.name)


def one(drill, kind, target, alert, expected, first_wait, open_wait, cap) -> dict:
    row = {"drill": drill, "kind": kind, "target": target, "expected": expected}
    tag = job_tag(drill)
    try:  # the undo runs even when the drill itself fails half-way
        print(pd.dip("drill", drill))
        time.sleep(first_wait)
        incident = wait_for(lambda: open_incident_id(kind, target), open_wait, tag=tag)
        row["incident"] = incident
        if incident is None:
            return row
        row["alert"] = alert[0]
        row["alert_seen"] = alert_seen(alert[0], alert[1], 420)
        row["chain"] = run_chain(incident, drill, cap)
        got = q("select status, root_cause, chain_id, reason from ops.agent_run"
                " where incident_id = %s", incident)  # fmt: skip
        if got:
            row["status"], row["root_cause"], row["chain_id"], row["reason"] = got[0]
        if row.get("chain_id"):
            cost = q("select coalesce(sum(calls), 0), coalesce(sum(cost_usd), 0)"
                     " from ops.agent_run_cost where chain_id = %s", row["chain_id"])  # fmt: skip
            row["model_calls"], row["cost_usd"] = int(cost[0][0]), float(cost[0][1])
            spans = q("select count(*) from ops.trace_span where chain_id = %s", row["chain_id"])
            row["trace_spans"] = int(spans[0][0])
    except Exception as exc:  # noqa: BLE001 - recorded on the row; the next drill still runs
        row["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        try:
            print(pd.dip("drill", drill, "--undo"))
        except Exception as exc:  # noqa: BLE001 - recorded; the next drill still runs
            row["undo_error"] = f"{type(exc).__name__}: {exc}"[:500]
        if row.get("incident"):

            def closed():
                if drill == "oom_task":  # a task queued before the pause can start a pod
                    pd.dip("drill", drill, "--undo")
                return not open_incident_id(kind, target)

            try:
                row["closed"] = bool(wait_for(closed, 420, every=15, tag=f"{tag}-after"))
            except Exception as exc:  # noqa: BLE001 - recorded; the next drill still runs
                row["undo_error"] = f"{type(exc).__name__}: {exc}"[:500]
    return row


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    total = float(os.environ.get("DIP_SPEND_CAP_USD", "0.15"))
    # each pod has its own meter: a share each, rounded down, keeps the run under its cap
    cap = f"{int(total / len(DRILLS) * 10000) / 10000:.4f}"
    rows = []
    model_key_secret(True)
    try:
        for spec in DRILLS:
            rows.append(one(*spec, cap=cap))
    finally:
        model_key_secret(False)

    def ok(r):
        return (not r.get("error") and not r.get("undo_error")
                and r.get("incident") is not None and r.get("alert_seen")
                and r.get("status") == "done" and r.get("root_cause") == r["expected"]
                and r.get("closed"))  # fmt: skip

    lines = ["# Stage 11 proof: the platform drills with the incident agent live", "",
             f"Run cap ${total:.2f} (each drill's chain ${cap}); every input synthetic.", "",
             "| Drill | Incident | Alert seen | Root cause (expected) | Model calls | Cost | "
             "Trace spans | Closed after undo | Result |",
             "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in rows:
        lines.append(
            f"| {r['drill']} | {r.get('incident')} {r['kind']} `{r['target']}` |"
            f" {r.get('alert')}: {'yes' if r.get('alert_seen') else 'no'} |"
            f" {r.get('root_cause')} ({r['expected']}) | {r.get('model_calls', 0)} |"
            f" ${r.get('cost_usd', 0):.4f} | {r.get('trace_spans', 0)} |"
            f" {'yes' if r.get('closed') else 'no'} | {'ok' if ok(r) else 'FAILED'} |")  # fmt: skip
    for r in rows:
        for k in ("error", "undo_error"):
            if r.get(k):
                lines.append(f"\n{r['drill']} {k}: `{r[k]}`")
    spent = sum(r.get("cost_usd", 0) for r in rows)
    lines += ["", f"Model spend, all drills: ${spent:.4f} (metered, ops.model_call)."]
    (out / "results.md").write_text("\n".join(lines) + "\n", "utf-8")
    (out / "results.json").write_text(json.dumps(rows, indent=1, default=str), "utf-8")
    print("\n".join(lines))
    return 0 if all(ok(r) for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
