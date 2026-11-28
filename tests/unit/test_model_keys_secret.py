"""24.39: no model key passes through Terraform (its state would keep it in plain text).
scripts/up.sh writes them from its environment into the dip-model-keys Secret, through
stdin, and only the agent runners' pods mount it."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra" / "terraform"


def test_no_terraform_resource_reads_a_model_key_variable():
    for path in TF.glob("*.tf"):
        if path.name == "variables.tf":
            continue
        text = path.read_text("utf-8")
        assert "var.anthropic_api_key" not in text and "var.gemini_api_key" not in text, path
        assert "ANTHROPIC_API_KEY" not in text and "GEMINI_API_KEY" not in text, path


def test_the_old_variables_refuse_a_value():
    text = (TF / "variables.tf").read_text("utf-8")
    for var in ("anthropic_api_key", "gemini_api_key"):
        block = re.search(f'variable "{var}"' + r" \{.*?\n\}", text, re.S)
        assert block, var
        assert f'condition     = var.{var} == ""' in block.group(0), var


def test_only_the_agent_runner_pods_mount_the_model_keys():
    src = (ROOT / "dags" / "agent_chain.py").read_text("utf-8")
    assert '(secret, "dip-model-keys")' in src
    for path in (ROOT / "dags").glob("*.py"):
        if path.name != "agent_chain.py":
            assert "dip-model-keys" not in path.read_text("utf-8"), path.name


def _secret_yaml(env: dict[str, str]) -> str:
    """Run up.sh's model_keys_secret function on its own, with this environment."""
    up = (ROOT / "scripts" / "up.sh").read_text("utf-8")
    fn = re.search(r"^model_keys_secret\(\) \{.*?^\}", up, re.S | re.M)
    assert fn
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    clean = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    out = subprocess.run([bash, "-c", fn.group(0) + "\nmodel_keys_secret"], env={**clean, **env},
                         capture_output=True, text=True, check=True)  # fmt: skip
    return out.stdout


def test_the_secret_holds_the_keys_base64_and_nothing_else():
    yml = _secret_yaml({"ANTHROPIC_API_KEY": "sk-test-not-a-key", "GEMINI_API_KEY": ""})
    assert "name: dip-model-keys" in yml and "namespace: dip-airflow" in yml
    enc = base64.b64encode(b"sk-test-not-a-key").decode()
    assert f"  ANTHROPIC_API_KEY: {enc}\n" in yml
    assert "GEMINI_API_KEY" not in yml
    assert "sk-test-not-a-key" not in yml


def test_without_keys_the_secret_is_empty():
    yml = _secret_yaml({})
    assert yml.rstrip().endswith("data:")
    assert "API_KEY" not in yml


def test_up_sh_sends_the_secret_through_stdin():
    up = (ROOT / "scripts" / "up.sh").read_text("utf-8")
    assert "model_keys_secret | kubectl" in up and "apply -f -" in up
    assert "--from-literal" not in up
