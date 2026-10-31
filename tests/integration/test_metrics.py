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


def test_the_agents_metrics(scraped, module_warehouse):
    """Stage 11 (S11.4): agent runs, fallback, decisions, spend and latency, from seeded
    rows, scraped as dip_metrics (whose grants cover exactly these columns)."""
    from datetime import timedelta

    from dip.agents import approvals, model_calls

    runner, _ = scraped
    wh = module_warehouse
    now = datetime.now(UTC)

    def call(run_id, provider="anthropic", status="ok", cost=0.01, ms=1000, source="live",
             later=0):  # fmt: skip
        r = {c: None for c in model_calls.COLUMNS}
        r.update(chain_id=run_id.rpartition("/")[0], run_id=run_id,
                 step=model_calls.step_of(run_id), provider=provider, source=source,
                 model="m", prompt_sha256="0" * 64, turn=1, status=status, input_tokens=1,
                 output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=cost,
                 started_at=now + timedelta(seconds=later), latency_ms=ms)  # fmt: skip
        r["finished_at"] = now + timedelta(milliseconds=ms)
        return r

    sink = model_calls.WarehouseSink(DSN)
    for r in (call("c1/triage"), call("c1/triage", ms=3000), call("c2/triage", status="error: X"),
              # a real restart: Claude failed (overloaded), then Gemini answered
              call("c3/map", status="error: APIStatusError"),
              call("c3/map@gemini", provider="gemini", cost=0, later=2),
              call("c5/map"),
              call("c4/map", source="recorded", cost=5.0)):  # fmt: skip
        sink(r)
    sink.close()
    a = approvals.create(wh.conn, "triage", "c1/triage", "discard", {"group_id": "g1"})
    approvals.decide(wh.conn, a, False, "human:tester")
    with wh.conn.cursor() as cur:
        cur.execute("insert into ops.intake_job (feed, mode, file_sha256, source, status)"
                    " values ('finance.x', 'onboard', %s, 'landing', 'failed')",
                    ("f" * 64,))  # fmt: skip
    wh.conn.commit()
    text = render(runner)
    for line in (
        'dip_agent_runs_24h{agent="triage",status="ok"} 1.0',
        'dip_agent_runs_24h{agent="triage",status="failed"} 1.0',
        'dip_agent_runs_24h{agent="map",status="ok"} 2.0',  # c3 (its restart won) and c5
        'dip_agent_runs_24h{agent="intake_team",status="failed"} 1.0',
        'dip_agent_fallback_runs_24h{agent="map"} 1.0',
        'dip_approvals_decided_7d{agent="triage",decision="rejected"} 1.0',
        # five live calls at $0.01; the replayed recording's $5 is not spend
        'dip_model_spend_usd_today{provider="anthropic"} 0.05',
        'dip_model_calls_24h{agent="triage",provider="anthropic"} 3.0',
        'dip_model_latency_p95_seconds_24h{agent="triage"} 2.8',
    ):
        assert line in text, line
    assert 'dip_metrics_query_ok{query="dip_agent_runs_24h"} 1' in text
    # the restart is one run that ended ok on the fallback, not a failure
    assert 'dip_agent_runs_24h{agent="map",status="failed"}' not in text
