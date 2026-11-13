"""13.4's scaling proof, run by the proof workflow's `scaling` job (no model key, no spend).

    python scripts/proof_scaling.py OUT_DIR

  1. the intake API's HPA reads its pod's CPU (metrics-server works) and it runs 1 pod;
  2. a load pod in dip-data (the intake API's own image, already in kind: nothing is pulled)
     calls the Service's /healthz from 16 threads, so the Service spreads it over the pods;
  3. the HPA scales the Deployment to 3 pods (its maximum);
  4. the load pod is deleted, and the HPA scales back to 1 (a 60 s stabilization window).

Each step's time goes in OUT_DIR/results.md and .json. Env: KUBECONFIG (context kind-dip),
INTAKE_API_IMAGE (dip-intake-api:<tag>, as up.sh loaded it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

NS, HPA, LOAD = "dip-data", "hpa/intake-api", "intake-api-load"
LOAD_SCRIPT = """
import threading, urllib.request
def hit():
    while True:
        try:
            urllib.request.urlopen("http://intake-api.dip-data.svc:8000/healthz", timeout=5).read()
        except Exception:
            pass
for _ in range(16):
    threading.Thread(target=hit, daemon=True).start()
threading.Event().wait()
"""


def kubectl(*args: str, check: bool = True) -> str:
    try:
        out = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                             timeout=120)  # fmt: skip
    except subprocess.TimeoutExpired as exc:  # one kind of error for the callers to handle
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: timed out") from exc
    if check and out.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def hpa() -> tuple[int, str]:
    """(current replicas, current CPU utilization or '')"""
    raw = kubectl("get", HPA, "-o", "json")
    status = json.loads(raw).get("status") or {}
    util = ""
    for m in status.get("currentMetrics") or []:
        u = ((m.get("resource") or {}).get("current") or {}).get("averageUtilization")
        if u is not None:
            util = str(u)
    return int(status.get("currentReplicas") or 0), util


def wait_for(what: str, ok, limit: float, started: float, log: list) -> bool:
    errors = 0
    while time.monotonic() - started < limit:
        try:
            replicas, util = hpa()
        except (RuntimeError, ValueError) as exc:
            # one API blip is retried; three in a row are a FAIL in the results, not a
            # crash that writes none
            errors += 1
            print(f"{what}: cannot read the HPA ({errors}): {exc}", file=sys.stderr)
            log.append({"t": round(time.monotonic() - started), "replicas": "?", "cpu": ""})
            if errors >= 3:
                return False
            time.sleep(10)
            continue
        errors = 0
        log.append({"t": round(time.monotonic() - started), "replicas": replicas, "cpu": util})
        if ok(replicas, util):
            print(f"{what}: yes after {log[-1]['t']} s ({replicas} pods, CPU {util}%)")
            return True
        time.sleep(10)
    print(f"{what}: NO within {limit:.0f} s", file=sys.stderr)
    return False


def main(out_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    image = os.environ["INTAKE_API_IMAGE"]
    log: list[dict] = []
    steps: dict[str, bool] = {}
    t0 = time.monotonic()
    steps["metrics: the HPA reads CPU, 1 pod"] = wait_for(
        "metrics", lambda r, u: u != "" and r == 1, 240, t0, log)  # fmt: skip
    container = {
        "name": LOAD,
        "image": image,
        "imagePullPolicy": "Never",
        "command": ["python", "-c", LOAD_SCRIPT],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"memory": "128Mi"},
        },
        "securityContext": {"runAsNonRoot": True, "allowPrivilegeEscalation": False},
    }
    overrides = json.dumps({"spec": {"containers": [container]}})
    t1 = time.monotonic()
    try:
        kubectl("run", LOAD, f"--image={image}", "--restart=Never", f"--overrides={overrides}")
        steps["up: 3 pods under load"] = wait_for("up", lambda r, u: r >= 3, 420, t1, log)
    except RuntimeError as exc:
        print(f"up: the load pod did not start: {exc}", file=sys.stderr)
        steps["up: 3 pods under load"] = False
    finally:
        try:
            kubectl("delete", "pod", LOAD, "--ignore-not-found", "--wait=false", check=False)
        except RuntimeError as exc:  # the job's teardown deletes the cluster anyway
            print(f"the load pod was not deleted: {exc}", file=sys.stderr)
    t2 = time.monotonic()
    if steps["up: 3 pods under load"]:
        steps["down: back to 1 pod after the load"] = wait_for(
            "down", lambda r, u: r == 1, 420, t2, log)  # fmt: skip
    else:  # 1 pod after no scale-up proves nothing
        steps["down: back to 1 pod after the load"] = False
    lines = ["# 13.4 scaling proof: the intake API's HPA", ""]
    lines += [f"- {'PASS' if ok else 'FAIL'}: {name}" for name, ok in steps.items()]
    lines += ["", "| seconds into the step | pods | CPU % of request |", "| --- | --- | --- |"]
    lines += [f"| {e['t']} | {e['replicas']} | {e['cpu'] or '?'} |" for e in log]
    (out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"steps": steps, "log": log}, indent=2),
                                      encoding="utf-8")  # fmt: skip
    print("\n".join(lines))
    return 0 if all(steps.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "artifacts/scaling"))
