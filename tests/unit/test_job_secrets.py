"""24.13: no Kubernetes Job spec in Terraform carries a password: MinIO root's credentials
and the passwords of the users the mc Jobs create come from Secrets (secret_key_ref, and
$(VAR) in args, which the kubelet expands), since anyone who can read Jobs could read them."""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def blocks(text: str, kind: str) -> list[tuple[str, str]]:
    """(name, body) of each `resource "<kind>" "<name>" { ... }`, by matching braces."""
    out = []
    for m in re.finditer(rf'resource "{kind}" "([^"]+)" \{{', text):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.append((m.group(1), text[m.end() : i]))
    return out


def test_no_job_spec_holds_a_password():
    bad = []
    seen = 0
    for f in sorted(TF.glob("*.tf")):
        for name, body in blocks(f.read_text(encoding="utf-8"), "kubernetes_job_v1"):
            seen += 1
            # A password's hash in a Job's name is fine (it renames the Job when it changes).
            spec = re.sub(r"sha256\([^()]*\)", "", body)
            if "local.pw[" in spec or "random_password" in spec:
                bad.append(f"{f.name}: {name}")
    assert seen >= 5
    assert bad == [], f"Jobs with a password in their spec: {bad}"


def test_the_mc_jobs_read_minio_root_from_its_secret():
    for f in ("minio.tf", "intake_api.tf", "mcp.tf", "backups.tf"):
        text = (TF / f).read_text(encoding="utf-8")
        assert 'name  = "MC_HOST_local"\n' not in text, f
        assert "kubernetes_secret_v1.minio_admin" in text, f
