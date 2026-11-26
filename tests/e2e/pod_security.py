"""24.15: Pod Security Admission on the deployed stack.

  1. dip-airflow enforces `restricted`; dip-data and dip-obs enforce `baseline`. All three
     warn and audit at `restricted` (infra/terraform/cluster.tf).
  2. `kubectl label --dry-run=server` asks the API server which running pods would fail
     `restricted`, without changing the namespace. The list is printed.
  3. The only pods allowed on that list: SFTP in dip-data (root, to chroot its users) and
     Marquez's two in dip-obs (upstream images that run as root). Every other pod, from
     this repo's Terraform or from the Airflow, Prometheus and Grafana charts, is at
     restricted.

Env: KUBECONFIG (the kind cluster's).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

# What each namespace enforces, and the only pods allowed to fall short of restricted there.
ENFORCE = {"dip-data": "baseline", "dip-airflow": "restricted", "dip-obs": "baseline"}
EXEMPT = {"dip-data": ("sftp-",), "dip-airflow": (), "dip-obs": ("marquez-",)}
WARNED = re.compile(
    r"^Warning: (?P<pod>[a-z0-9][a-z0-9.-]*)(?: \(and (?P<more>\d+) other pods?\))?: "
)


def kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True,
                          text=True, timeout=120)  # fmt: skip


def main() -> int:
    failures = []
    for ns in ENFORCE:
        got = kubectl("get", "namespace", ns, "-o", "json")
        if got.returncode:
            if ns != "dip-obs":  # only the observability profile can be switched off
                failures.append(f"{ns}: not found: {got.stderr.strip()[-200:]}")
            else:
                print(f"{ns}: not deployed (observability profile off), skipped")
            continue
        labels = json.loads(got.stdout)["metadata"].get("labels", {})
        for mode, level in (("enforce", ENFORCE[ns]), ("warn", "restricted"),
                            ("audit", "restricted")):  # fmt: skip
            if labels.get(f"pod-security.kubernetes.io/{mode}") != level:
                failures.append(f"{ns}: pod-security {mode} is not {level}")
        dry = kubectl("label", "--dry-run=server", "--overwrite", "namespace", ns,
                      "pod-security.kubernetes.io/enforce=restricted")  # fmt: skip
        if dry.returncode:
            failures.append(f"{ns}: the dry run failed: {dry.stderr.strip()[-300:]}")
            continue
        pods = [m["pod"] for line in dry.stderr.splitlines() if (m := WARNED.match(line))]
        print(f"{ns}: {len(pods)} pod(s) would fail restricted: {', '.join(pods) or 'none'}")
        for line in dry.stderr.splitlines():
            if WARNED.match(line):
                print(f"  {line}")
        failures += [f"{ns}/{p} fails restricted" for p in pods if not p.startswith(EXEMPT[ns])]
        # The API server folds pods with the same violations into one line ("x (and N other
        # pods)"), so a pod failing exactly as SFTP or Marquez does could hide behind their
        # name. Count every pod the warnings cover against the exempt pods that exist.
        covered = sum(1 + int(m["more"] or 0) for line in dry.stderr.splitlines()
                      if (m := WARNED.match(line)))  # fmt: skip
        names = kubectl("-n", ns, "get", "pods", "-o", "jsonpath={.items[*].metadata.name}")
        exempt = sum(1 for n in names.stdout.split() if n.startswith(EXEMPT[ns]))
        if covered > exempt and not any(f.startswith(f"{ns}/") for f in failures):
            failures.append(f"{ns}: {covered} pods fail restricted, only {exempt} are exempt")
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("pod security: enforced, and every pod but SFTP and Marquez is at restricted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
