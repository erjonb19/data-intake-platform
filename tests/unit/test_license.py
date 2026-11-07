"""Stage 12 (12.4, #84): the repository is Apache-2.0. LICENSE is the license text unchanged,
pyproject.toml declares it, and NOTICE names the third-party chart committed with its own
license."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def test_license_is_apache_2_unchanged() -> None:
    text = (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(text).hexdigest() == APACHE_2_SHA256


def test_pyproject_declares_apache_2() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["license"] == "Apache-2.0"
    # An explicit license-files list would fail the image builds, which copy only
    # pyproject.toml and dip/; setuptools' default patterns pick LICENSE and NOTICE up.
    assert "license-files" not in meta["project"]


def test_notice_names_the_bundled_chart_and_its_license() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Apache License, Version 2.0" in notice
    for chart in (ROOT / "infra" / "helm-charts").glob("*.tgz"):
        assert f"infra/helm-charts/{chart.name}" in notice
    assert (ROOT / "infra" / "helm-charts" / "airflow-license" / "LICENSE").is_file()
