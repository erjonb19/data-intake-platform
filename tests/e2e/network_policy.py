"""24.16: the NetworkPolicies are enforced, not just present.

Two short-lived pods on the Postgres image (already on the node; it has pg_isready and
busybox wget) try Postgres (5432) and MinIO's API (9000) in dip-data:
  1. one in `default`, outside the platform's namespaces: both must be refused;
  2. one in dip-airflow labelled `component=worker`, as Airflow's task pods are: both must
     answer.
Without (2), a CNI that enforced nothing would pass (1) only if the services were down;
without (1), a CNI that ignored NetworkPolicy would pass (2).

Env: KUBECONFIG (the kind cluster's).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

SCRIPT = (
    "if pg_isready -q -h postgres.dip-data -p 5432 -t 4; then echo postgres=open;"
    " else echo postgres=closed; fi;"
    " if wget -q -T 4 -O /dev/null http://minio.dip-data:9000/minio/health/live;"
    " then echo minio=open; else echo minio=closed; fi"
)


def kubectl(*args: str, check: bool = True) -> str:
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=180)
    if check and out.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()[-400:]}")
    return out.stdout


def probe(namespace: str, name: str, labels: str, image: str) -> dict[str, str]:
    kubectl("-n", namespace, "delete", "pod", name, "--ignore-not-found", "--wait=true")
    kubectl("-n", namespace, "run", name, f"--image={image}", "--restart=Never",
            f"--labels={labels}", "--command", "--", "sh", "-c", SCRIPT)  # fmt: skip
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        phase = kubectl("-n", namespace, "get", "pod", name, "-o", "jsonpath={.status.phase}")
        if phase in ("Succeeded", "Failed"):
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"{namespace}/{name} did not finish in 120 s")
    logs = kubectl("-n", namespace, "logs", name)
    kubectl("-n", namespace, "delete", "pod", name, "--ignore-not-found", "--wait=false")
    result = dict(line.split("=", 1) for line in logs.split() if "=" in line)
    print(f"{namespace} ({labels}): {result}")
    return result


def main() -> int:
    sts = json.loads(kubectl("-n", "dip-data", "get", "statefulset", "postgres", "-o", "json"))
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    outside = probe("default", "netpol-outside", "run=netpol-outside", image)
    worker = probe("dip-airflow", "netpol-worker", "run=netpol-worker,component=worker", image)
    failures = [f"a pod in default reached {svc}" for svc, v in outside.items() if v != "closed"]
    failures += [f"an Airflow task pod could not reach {svc}" for svc, v in worker.items()
                 if v != "open"]  # fmt: skip
    if set(outside) != {"postgres", "minio"} or set(worker) != {"postgres", "minio"}:
        failures.append("a probe pod did not report both services")
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("network policies: enforced (outside pod refused, task pod allowed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
