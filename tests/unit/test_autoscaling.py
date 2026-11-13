"""13.4: metrics-server and the two HPAs, as Terraform defines them."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra" / "terraform"


def text(name: str) -> str:
    return (TF / name).read_text(encoding="utf-8")


def test_metrics_server_and_the_hpas_share_one_switch():
    auto = text("autoscaling.tf")
    assert "count      = var.metrics_server_enabled ? 1 : 0" in auto
    assert "for_each = var.metrics_server_enabled ? local.autoscaled : {}" in auto
    assert re.search(
        r'variable "metrics_server_enabled" \{[^}]*type\s*=\s*bool', text("variables.tf")
    )


def test_each_scaled_deployment_has_a_cpu_request_and_leaves_replicas_to_the_hpa():
    """An HPA on CPU utilization needs a CPU request; Terraform must not reset its count."""
    for fname, name in (("intake_api.tf", "intake_api"), ("mcp.tf", "mcp")):
        t = text(fname)
        start = t.index(f'resource "kubernetes_deployment_v1" "{name}" {{')
        block = t[start : t.index("\n}\n", start)]
        assert re.search(r'requests = \{ cpu = "\d+m"', block), f"{name}: no CPU request"
        assert "ignore_changes = [spec[0].replicas]" in block, f"{name}: replicas not ignored"


def test_the_mcp_server_keeps_no_session_in_the_pod():
    """Review of #163: with 2 or 3 MCP pods behind a Service with no affinity, in-memory
    sessions would 404 on another pod. Stateless HTTP: every request stands alone."""
    src = (ROOT / "dip/agents/mcp_server.py").read_text(encoding="utf-8")
    assert "stateless_http=True," in src


def test_the_hpas_scale_one_to_three():
    auto = text("autoscaling.tf")
    assert "min_replicas = 1" in auto and "max_replicas = 3" in auto
    assert '"intake-api" =' in auto and '"mcp"        =' in auto


def test_the_metrics_server_image_is_pinned_by_digest_and_sized_small():
    values = yaml.safe_load((ROOT / "infra/helm-values/metrics-server.yaml").read_text("utf-8"))
    assert re.fullmatch(r"v[\d.]+@sha256:[0-9a-f]{64}", values["image"]["tag"])
    assert values["resources"]["limits"]["memory"] == "128Mi"
    assert "--kubelet-insecure-tls" in values["args"]  # kind's self-signed kubelets


def test_the_proof_workflow_runs_the_scaling_proof_without_a_model_key():
    wf = yaml.safe_load((ROOT / ".github/workflows/proof.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["scaling"]
    assert job["if"] == "inputs.job == 'scaling'"
    assert "scaling" in wf[True]["workflow_dispatch"]["inputs"]["job"]["options"]
    assert "secrets." not in yaml.safe_dump(job)  # no key, no spend
