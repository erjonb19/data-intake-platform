"""24.14: the postgres-audit-owner Job runs postgres-audit-owner.sql as the superuser in the
warehouse database, after warehouse-init has created the tables, and on every apply."""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def job() -> str:
    text = (TF / "platform_check.tf").read_text(encoding="utf-8")
    start = text.index('resource "kubernetes_job_v1" "postgres_audit_owner"')
    end = text.index("\nresource ", start + 1) if "\nresource " in text[start + 1 :] else None
    return text[start:end]


def test_it_runs_the_file_as_the_superuser_in_the_warehouse_database():
    body = job()
    assert '"-f", "/sql/audit-owner.sql"' in body
    assert '"-1"' in body  # one transaction: all of it or none
    assert re.search(r'name\s*=\s*"PGUSER"\s*value\s*=\s*"postgres"', body)
    assert re.search(r'name\s*=\s*"PGDATABASE"\s*value\s*=\s*"warehouse"', body)
    assert "kubernetes_namespace_v1.data" in body  # beside Postgres, not in an Airflow pod
    roles = (TF / "postgres.tf").read_text(encoding="utf-8")
    assert '"audit-owner.sql" = file("${path.module}/files/postgres-audit-owner.sql")' in roles


def test_it_runs_after_the_migrations_and_on_every_apply():
    body = job()
    assert "depends_on = [kubernetes_job_v1.warehouse_init]" in body
    assert "replace_triggered_by = [terraform_data.every_apply" in body
