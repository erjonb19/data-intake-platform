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


def test_a_container_without_probes_is_caught():
    fake = 'resource "kubernetes_deployment_v1" "x" { spec { container { name = "a" } } }'
    (c,) = containers(block(fake, fake.index("{")))
    assert "liveness_probe {" not in c
