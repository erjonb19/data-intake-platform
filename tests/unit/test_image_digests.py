"""24.22: every image the platform runs is pinned by digest, so a moved tag cannot change
what is deployed. Dependabot's docker updates keep the digests current."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def test_every_image_default_in_terraform_is_pinned_by_digest():
    text = (ROOT / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    images = re.findall(r'variable "(\w*image)" \{.*?default\s*=\s*"([^"]+)"', text, re.S)
    assert len(images) >= 7
    bad = [f"{v} = {d}" for v, d in images if not DIGEST.search(d)]
    assert bad == [], f"not pinned by digest: {bad}"


def test_every_dockerfile_base_is_pinned_by_digest():
    bad = []
    for f in sorted((ROOT / "images").glob("*/Dockerfile")):
        for line in f.read_text(encoding="utf-8").splitlines():
            # ocr's `FROM ${BASE}` is the Airflow image built here
            if line.startswith("FROM ") and "${" not in line and not DIGEST.search(line.split()[1]):
                bad.append(f"{f.parent.name}: {line}")
    assert bad == [], f"not pinned by digest: {bad}"
