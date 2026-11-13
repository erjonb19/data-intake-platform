"""13.1: every container of every Deployment and StatefulSet that Terraform defines has a
liveness and a readiness probe. (The Helm charts' own workloads, Airflow, Prometheus and
Grafana, carry their charts' default probes, which the repo's values leave on.)"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parents[2] / "infra" / "terraform"
WORKLOAD = re.compile(r'^resource "kubernetes_(deployment|stateful_set)_v1" "(\w+)" \{', re.M)


def block(text: str, start: int) -> str:
    """The text from `start` (an opening brace) to its matching closing brace."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError("unbalanced braces")


def containers(resource: str) -> list[str]:
    """The `container { ... }` blocks (not `init_container`)."""
    return [block(resource, m.end() - 1)
            for m in re.finditer(r"(?<![\w_])container \{", resource)]  # fmt: skip


def workloads() -> dict[str, str]:
    found = {}
    for tf in sorted(TERRAFORM.glob("*.tf")):
        text = tf.read_text(encoding="utf-8")
        for m in WORKLOAD.finditer(text):
            found[f"{tf.name}:{m.group(2)}"] = block(text, m.end() - 1)
    return found


def test_the_workloads_are_found():
    names = {k.split(":")[1] for k in workloads()}
    assert {"approvals", "intake_api", "marquez", "marquez_web", "mcp", "minio", "metrics",
            "postgres", "sftp"} <= names  # fmt: skip


def test_every_container_has_liveness_and_readiness_probes():
    missing = []
    for name, resource in workloads().items():
        cs = containers(resource)
        assert cs, f"{name}: no container block found"
        for c in cs:
            cname = re.search(r'name\s*=\s*"([^"]+)"', c)
            label = f"{name}/{cname.group(1) if cname else '?'}"
            for probe in ("liveness_probe", "readiness_probe"):
                if f"{probe} {{" not in c:
                    missing.append(f"{label}: no {probe}")
    assert missing == [], "\n".join(missing)


def test_http_probes_on_a_host_checked_server_send_an_allowed_host():
    """A server started with `--allowed-host` (the approvals gateway) answers 400 to the
    kubelet's default Host, the pod IP: an HTTP probe there must send an allowed Host, or a
    liveness probe restarts the pod in a loop (found in review, #159)."""
    checked = 0
    for name, resource in workloads().items():
        for c in containers(resource):
            allowed = set(re.findall(r'"--allowed-host",\s*"([^"]+)"', c))
            if not allowed:
                continue
            checked += 1
            for m in re.finditer(r"(liveness|readiness|startup)_probe \{", c):
                probe = block(c, m.end() - 1)
                if "http_get {" in probe:
                    host = re.search(r'name\s*=\s*"Host"\s*value\s*=\s*"([^"]+)"', probe)
                    assert host, f"{name}: an HTTP {m.group(1)} probe without a Host header"
                    assert host.group(1) in allowed, (
                        f"{name}: probe Host {host.group(1)} is not an --allowed-host"
                    )
    assert checked, "no container with --allowed-host found: did the gateway's args move?"


def test_a_container_without_probes_is_caught():
    fake = 'resource "kubernetes_deployment_v1" "x" { spec { container { name = "a" } } }'
    (c,) = containers(block(fake, fake.index("{")))
    assert "liveness_probe {" not in c
