"""24.30: one variable rotates every MCP key and the run token key (docs/keys.md)."""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def test_every_mcp_key_is_replaced_when_the_generation_changes():
    text = (TF / "mcp.tf").read_text(encoding="utf-8")
    blocks = re.findall(r'resource "random_password" "(\w+)" \{(.*?)\n\}', text, re.S)
    names = {name for name, _ in blocks}
    assert {"run_token_key", "mcp_reader_key", "mcp_triage_key", "mcp_incident_key",
            "mcp_team_key"} <= names  # fmt: skip
    for name, body in blocks:
        assert re.search(r"keepers\s*=\s*\{\s*generation\s*=\s*var\.mcp_key_generation\s*\}",
                         body), name  # fmt: skip


def test_the_mcp_server_rolls_when_its_keys_change():
    text = (TF / "mcp.tf").read_text(encoding="utf-8")
    assert '"dip/config-sha" = sha256(jsonencode(kubernetes_secret_v1.mcp.data))' in text
