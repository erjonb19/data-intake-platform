"""13.2: every stateless Deployment rolls with `maxUnavailable: 0`, and `dip rollback` undoes
a rollout on the kind cluster only."""

from __future__ import annotations

import re

import pytest

from dip import rollback
from dip.drills import DrillError
from tests.unit.test_probes import TERRAFORM, block, workloads


class Kubectl:
    def __init__(self, context="kind-dip"):
        self.context, self.calls = context, []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ("config", "current-context"):
            return self.context
        if "jsonpath={.spec.template.spec.containers[0].image}" in args:
            n = sum("jsonpath={.spec.template.spec.containers[0].image}" in c for c in self.calls)
            return "dip-intake-api:bad" if n == 1 else "dip-intake-api:good"
        return ""


def strategy(resource: str) -> str:
    m = re.search(r"\n    strategy \{", resource)
    return block(resource, m.end() - 1) if m else ""


def deployments() -> dict[str, str]:
    return {k: v for k, v in workloads().items()
            if f'resource "kubernetes_deployment_v1" "{k.split(":")[1]}"' in
            (TERRAFORM / k.split(":")[0]).read_text(encoding="utf-8")}  # fmt: skip


def test_every_deployment_rolls_without_downtime_or_recreates_for_its_volume():
    wrong = []
    for name, resource in deployments().items():
        s = strategy(resource)
        if '"Recreate"' in s:
            # Only for a ReadWriteOnce volume: two pods cannot both mount it.
            if "persistent_volume_claim {" not in resource:
                wrong.append(f"{name}: Recreate without a volume that needs it")
        elif not ('type = "RollingUpdate"' in s and re.search(r'max_unavailable\s*=\s*"0"', s)):
            wrong.append(f"{name}: no RollingUpdate with max_unavailable = 0")
    assert wrong == [], "\n".join(wrong)


def test_rollback_offers_only_rolling_deployments_terraform_defines():
    """Each service maps to a real Deployment (namespace and name), and none of them uses
    Recreate: undoing a Recreate rollout takes the service down first."""
    defined = {}
    for resource in deployments().values():
        name = re.search(r'metadata \{\s*name\s*=\s*"([^"]+)"', resource)
        ns = re.search(r"namespace\s*=\s*kubernetes_namespace_v1\.(\w+)", resource)
        assert name and ns
        key = ({"data": "dip-data", "obs": "dip-obs"}[ns.group(1)], name.group(1))
        defined[key] = strategy(resource)
    for service, pair in rollback.SERVICES.items():
        assert pair in defined, f"{service}: {pair} is not a Deployment in infra/terraform"
        assert 'type = "RollingUpdate"' in defined[pair], f"{service} does not roll"


def test_rollback_undoes_and_waits_for_ready():
    k = Kubectl()
    out = rollback.rollback("intake-api", run=k)
    assert ("-n", "dip-data", "rollout", "undo", "deployment/intake-api") in k.calls
    assert ("-n", "dip-data", "rollout", "status", "deployment/intake-api",
            "--timeout=180s") in k.calls  # fmt: skip
    assert "from dip-intake-api:bad to dip-intake-api:good" in out


def test_rollback_refuses_any_cluster_but_kind():
    k = Kubectl(context="prod-eu")
    with pytest.raises(DrillError, match="not the kind cluster"):
        rollback.rollback("intake-api", run=k)
    assert all(c[:2] == ("config", "current-context") for c in k.calls)


def test_rollback_refuses_an_unknown_service():
    k = Kubectl()
    with pytest.raises(DrillError, match="unknown service"):
        rollback.rollback("minio", run=k)
    assert k.calls == []
