"""S3.2: the metrics exporter reads the ops tables as the read-only dip_metrics login, and
nothing else. Applies the real role file, lands the finance register, builds its dbt models
(which record DQ results), opens a late-file incident, then scrapes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, sla
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.metrics import _Runner, render
from dip.storage import LocalStore
from tests.integration.conftest import DSN

ROLES_SQL = REPO_ROOT / "infra/terraform/files/postgres-roles.sql"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
# A fixed, public test password: fine for a throwaway CI database, never for a deployment.
PASSWORD = "ci-metrics-0123456789"


def metrics_dsn() -> str:
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(DSN)
    info.update(user="dip_metrics", password=PASSWORD)
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture(scope="module")
def scraped(module_warehouse, tmp_path_factory):
    wh = module_warehouse
    with wh.conn.cursor() as cur:
        cur.execute(ROLES_SQL.read_text(encoding="utf-8"))
        cur.execute(f"alter role dip_metrics password '{PASSWORD}'")
    wh.conn.commit()
    wh.init()  # the role exists now: the grants on the intake tables apply
    store = LocalStore(tmp_path_factory.mktemp("store"))
    for name in ("good/invoices_good.xlsx", "bad/invoices_sheet_renamed.xlsx"):
        data = (FIXTURES / name).read_bytes()
        store.put(BUCKET_LANDING, f"finance.vendor_invoice/{Path(name).name}", data)
    process_landing(store, wh, load_contracts(), datetime.now(UTC))
    feed = "finance.vendor_invoice"
    assert run_dbt(DSN, "build", "--select", f"tag:{feed}", check=False).returncode == 0
    with wh.conn.cursor() as cur:
        cur.execute(sla.DDL)
        cur.execute(
            "insert into ops.incident (feed, kind, window_start, since)"
            " values ('healthcare.claims', 'FEED.LATE', now() - interval '1 day', now())"
        )
    wh.conn.commit()
    runner = _Runner(metrics_dsn())
    yield runner, render(runner)
    runner.conn.close()


def test_every_query_runs_as_the_metrics_login(scraped):
    _, text = scraped
    assert 'dip_metrics_query_ok{query="dip_intake_files"} 1' in text
    assert " 0\n" not in "".join(ln + "\n" for ln in text.splitlines() if "query_ok" in ln)


def test_the_values_are_the_warehouse_state(scraped):
    _, text = scraped
    feed = 'feed="finance.vendor_invoice"'
    assert f'dip_intake_files{{{feed},status="loaded"}} 1.0' in text
    assert f'dip_intake_files{{{feed},status="quarantined"}} 1.0' in text
    assert f'dip_rows_24h{{{feed},outcome="accepted"}} 12.0' in text
    assert f'dip_quarantine_open{{{feed},reason_code="SCHEMA.MISSING_SHEET:Invoices"}} 1.0' in text
    assert 'dip_incidents_open{feed="healthcare.claims",kind="FEED.LATE"} 1.0' in text
    assert 'dip_dq_test_status{test_name="invoice_totals_add_up",dq_category=' in text


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from bronze.csv_record",
        "select detail from ops.quarantine",
        "select record_ref from ops.quarantine",
        "select source_key from ops.intake_file",
        "select count(*) from gold.vendor_invoice",
        "select count(*) from ops.audit_log",
    ],
)
def test_the_login_reads_nothing_else(scraped, sql):
    runner, _ = scraped
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        runner(sql)


def test_the_login_is_read_only(scraped):
    runner, _ = scraped
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        runner("create temp table t (x int)")
