"""24.38 (part 1): Postgres serves TLS and takes TCP logins only over it."""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def test_pg_hba_takes_tcp_logins_only_over_tls():
    tls = (TF / "tls.tf").read_text("utf-8")
    hba = re.search(r'"pg_hba.conf" = <<-EOT\n(.*?)\n\s*EOT', tls, re.S).group(1)
    rules = [ln.split() for ln in hba.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert rules == [["local", "all", "all", "trust"],
                     ["hostssl", "all", "all", "all", "scram-sha-256"]]  # fmt: skip


def test_the_server_turns_tls_on_with_its_key_copied_private():
    pg = (TF / "postgres.tf").read_text("utf-8")
    for arg in ('"ssl=on"', '"ssl_key_file=/tls/server.key"', '"hba_file=/etc/dip-pg/pg_hba.conf"',
                '"ssl_min_protocol_version=TLSv1.2"'):  # fmt: skip
        assert arg in pg, arg
    assert "install -m 0600 /tls-src/tls.key /tls/server.key" in pg


def test_the_certificate_names_every_way_clients_reach_postgres():
    tls = (TF / "tls.tf").read_text("utf-8")
    for name in ("local.postgres_host", '"postgres"', '"localhost"'):
        assert name in tls, name
    assert '"server_auth"' in tls


def test_marquez_asks_for_tls():
    assert "/marquez?sslmode=require" in (TF / "marquez.tf").read_text("utf-8")
