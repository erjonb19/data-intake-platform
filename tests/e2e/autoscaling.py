"""13.4, in every E2E: metrics-server works and both HPAs read their pods' CPU (an HPA
without metrics shows `<unknown>` and never scales). The load test that scales the intake
API from 1 to 3 pods and back is the proof workflow's `scaling` job (scripts/proof_scaling.py).

Env: KUBECONFIG (context kind-dip). Skipped, with a note, when metrics_server_enabled is false
(no HPAs).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

HPAS = ("intake-api", "mcp")


def utilization(name: str) -> int | None:
    out = subprocess.run(["kubectl", "-n", "dip-data", "get", f"hpa/{name}", "-o", "json"],
                         capture_output=True, text=True, timeout=60)  # fmt: skip
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[-300:])
    for m in (json.loads(out.stdout).get("status") or {}).get("currentMetrics") or []:
        u = ((m.get("resource") or {}).get("current") or {}).get("averageUtilization")
        if u is not None:
            return int(u)
    return None


def main() -> int:
    listed = subprocess.run(["kubectl", "-n", "dip-data", "get", "hpa", "-o", "name"],
                            capture_output=True, text=True, timeout=60)  # fmt: skip
    if listed.returncode:
        print(f"FAIL: cannot list HPAs: {listed.stderr.strip()[-300:]}", file=sys.stderr)
        return 1
    if not listed.stdout.strip():
        print("no HPAs (metrics_server_enabled is false): skipped")
        return 0
    started, seen = time.monotonic(), {}
    while len(seen) < len(HPAS) and time.monotonic() - started < 240:
        for name in HPAS:
            if name not in seen and (u := utilization(name)) is not None:
                seen[name] = u
        if len(seen) < len(HPAS):
            time.sleep(10)
    for name in HPAS:
        state = f"CPU {seen[name]}% of its request" if name in seen else "no metrics"
        print(f"==> HPA {name}: {state}: {'ok' if name in seen else 'FAILED'}")
    return 0 if len(seen) == len(HPAS) else 1


if __name__ == "__main__":
    sys.exit(main())
