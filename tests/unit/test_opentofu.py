"""13.5: CI checks that infra/terraform/ also works under OpenTofu, the open-source fork."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_the_lint_job_runs_tofu_init_and_validate_on_the_terraform_code():
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["lint"]["steps"]
    setup = [s for s in steps if str(s.get("uses", "")).startswith("opentofu/setup-opentofu@")]
    assert len(setup) == 1
    assert setup[0]["with"]["tofu_version"].count(".") == 2  # an exact version, not latest
    assert setup[0]["with"]["tofu_wrapper"] is False
    run = [s for s in steps if "tofu validate" in s.get("run", "")]
    assert len(run) == 1
    assert run[0]["working-directory"] == "infra/terraform"
    assert "tofu init -backend=false" in run[0]["run"]
    # its own data directory: nothing shared with Terraform's .terraform/
    assert run[0]["env"]["TF_DATA_DIR"] == ".tofu"
