"""24.2: Airflow pods stop holding the master secrets.

Every Airflow pod (API server, scheduler, each task pod) gets the chart's `secret:` list, and
task pods parse untrusted vendor files. So that list holds only the two connections, the
minio connection is Airflow's own bucket-scoped MinIO user (not root), and the
de-identification and SFTP keys reach only the one task that needs each."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VALUES = yaml.safe_load((ROOT / "infra" / "helm-values" / "airflow.yaml").read_text("utf-8"))
AIRFLOW_TF = (ROOT / "infra" / "terraform" / "airflow.tf").read_text("utf-8")


def test_global_secret_list_holds_only_the_connections() -> None:
    names = {s["envName"] for s in VALUES["secret"]}
    assert names == {"AIRFLOW_CONN_MINIO", "AIRFLOW_CONN_WAREHOUSE"}
    assert not {s["secretName"] for s in VALUES["secret"]} & {"dip-deid", "dip-sftp"}


def test_minio_connection_is_not_root() -> None:
    conn = re.search(r"minio = jsonencode\(\{(.*?)\}\)", AIRFLOW_TF, re.S)
    assert conn, "the minio connection is not in airflow.tf"
    body = conn.group(1)
    assert "local.airflow_minio_user" in body
    assert 'local.pw["minio_airflow"]' in body
    assert "minio_root" not in body


def test_airflow_minio_user_cannot_reach_backups() -> None:
    buckets = re.search(r"airflow_minio_buckets\s*=\s*(\[.*?\])", AIRFLOW_TF)
    assert buckets
    assert json.loads(buckets.group(1)) == ["landing", "raw", "quarantine", "airflow-logs"]
    assert "backups" not in buckets.group(1)


def _task_executor_configs(dag_file: str) -> dict[str, str]:
    """Task function name -> the name of the function its executor_config calls."""
    tree = ast.parse((ROOT / "dags" / dag_file).read_text("utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "executor_config" and isinstance(kw.value, ast.Call):
                        found[node.name] = kw.value.func.id  # type: ignore[attr-defined]
    return found


def test_deid_key_only_in_the_fhir_transform_task() -> None:
    assert _task_executor_configs("fhir_intake.py") == {"transform": "_deid_key_pod"}
    src = (ROOT / "dags" / "fhir_intake.py").read_text("utf-8")
    assert 'name="DIP_DEID_KEY"' in src and 'name="dip-deid"' in src


def test_sftp_key_only_in_the_sync_task() -> None:
    assert _task_executor_configs("sftp_sync.py") == {"sync": "_sftp_pod"}
    src = (ROOT / "dags" / "sftp_sync.py").read_text("utf-8")
    for env in ("DIP_SFTP_HOST", "DIP_SFTP_HOST_KEY", "DIP_SFTP_KEY", "DIP_SFTP_ACCOUNTS"):
        assert f'"{env}"' in src
    assert 'name="dip-sftp"' in src


def test_no_other_dag_mounts_the_keys() -> None:
    for path in (ROOT / "dags").glob("*.py"):
        if path.name in {"fhir_intake.py", "sftp_sync.py"}:
            continue
        src = path.read_text("utf-8")
        assert "dip-deid" not in src and "dip-sftp" not in src, path.name
