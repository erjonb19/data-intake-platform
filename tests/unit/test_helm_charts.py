"""The Airflow Helm chart is committed (infra/helm-charts/) so no deploy downloads it
(incident 2026-11-06, docs/incidents.md). The committed file must be the upstream release:
it matches Apache's .sha512, Terraform deploys exactly that file, and its license travels
with it."""

from __future__ import annotations

import hashlib
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHARTS = ROOT / "infra" / "helm-charts"
TF = ROOT / "infra" / "terraform"


def chart_version() -> str:
    variables = (TF / "variables.tf").read_text(encoding="utf-8")
    found = re.search(r'variable "airflow_chart_version" \{.*?default\s*=\s*"([^"]+)"',
                      variables, re.S)  # fmt: skip
    assert found
    return found.group(1)


def test_chart_matches_upstream_sha512() -> None:
    chart = CHARTS / f"airflow-{chart_version()}.tgz"
    expected = (CHARTS / f"{chart.name}.sha512").read_text(encoding="utf-8").split()[0]
    assert hashlib.sha512(chart.read_bytes()).hexdigest() == expected


def test_only_the_deployed_version_is_committed() -> None:
    assert sorted(p.name for p in CHARTS.glob("*.tgz")) == [f"airflow-{chart_version()}.tgz"]


def test_chart_is_the_version_it_claims() -> None:
    with tarfile.open(CHARTS / f"airflow-{chart_version()}.tgz") as tgz:
        meta = tgz.extractfile("airflow/Chart.yaml").read().decode()
        license_in_chart = tgz.extractfile("airflow/LICENSE").read()
    assert re.search(rf"^version: {re.escape(chart_version())}$", meta, re.M)
    assert (CHARTS / "airflow-license" / "LICENSE").read_bytes() == license_in_chart
    assert (CHARTS / "airflow-license" / "NOTICE").is_file()


def test_terraform_deploys_the_local_file_with_its_checksum() -> None:
    tf = (TF / "airflow.tf").read_text(encoding="utf-8")
    release = tf.split('resource "helm_release" "airflow"', 1)[1]
    assert not re.search(r"^  repository\s*=", release.split("\n}\n", 1)[0], re.M)
    assert "chart     = local.airflow_chart" in release
    assert "helm-charts/airflow-${var.airflow_chart_version}.tgz" in tf
    assert "filesha512(local.airflow_chart) == local.airflow_chart_sha512" in release
