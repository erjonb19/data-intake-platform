"""24.15: Pod Security Admission on the deployed stack.

  1. Each platform namespace enforces `baseline` and warns and audits at `restricted`
     (infra/terraform/cluster.tf).
  2. `kubectl label --dry-run=server` asks the API server which running pods would fail
     `restricted`, without changing the namespace. The list is printed: it is the work left
     before a namespace can enforce restricted.
  3. None of them is a pod this repo's Terraform defines and hardens. Postgres, MinIO, the
     intake API, the MCP server, the approvals page, the metrics exporter, the backup and
     platform-check CronJobs and the setup Jobs all run non-root, drop every capability,
     use the RuntimeDefault seccomp profile and allow no privilege escalation. What may still
     be listed: SFTP (root, to chroot its users), Marquez, and the Airflow, Prometheus and
     Grafana charts' pods.

Env: KUBECONFIG (the kind cluster's).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

NAMESPACES = ("dip-data", "dip-airflow", "dip-obs")
OURS = ("postgres-", "minio-", "intake-api-", "mcp-", "approvals-", "dip-metrics-",
        "warehouse-backup-", "platform-check-", "warehouse-init-", "postgres-roles-",
        "postgres-audit-owner-", "airflow-mcp-viewer-")  # fmt: skip
WARNED = re.compile(r"^Warning: (?P<pod>[a-z0-9][a-z0-9.-]*)(?: \(and \d+ other pods?\))?: ")


def kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True,
                          text=True, timeout=120)  # fmt: skip


def main() -> int:
    failures = []
    for ns in NAMESPACES:
        got = kubectl("get", "namespace", ns, "-o", "json")
        if got.returncode:
            if ns != "dip-obs":  # only the observability profile can be switched off
                failures.append(f"{ns}: not found: {got.stderr.strip()[-200:]}")
            else:
                print(f"{ns}: not deployed (observability profile off), skipped")
            continue
        labels = json.loads(got.stdout)["metadata"].get("labels", {})
        for mode, level in (("enforce", "baseline"), ("warn", "restricted"),
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
        failures += [f"{ns}/{p} is ours and fails restricted" for p in pods if p.startswith(OURS)]
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("pod security: baseline enforced, and every pod this repo defines is at restricted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
