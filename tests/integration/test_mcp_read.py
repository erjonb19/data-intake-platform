"""S3.4: the MCP Read tier's warehouse-backed tools, as the dip_mcp login the deployed server
uses. Applies the real role file, lands two eligibility files, builds their dbt models (for
DQ results), then calls the tools through the registry with the reader's policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING
from dip.agents.read_tools import ReadServices
from dip.agents.registry import Policy
from dip.agents.tools import build_registry
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN

ROLES_SQL = REPO_ROOT / "infra/terraform/files/postgres-roles.sql"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"
# A fixed, public test password: fine for a throwaway CI database, never for a deployment.
PASSWORD = "ci-mcp-0123456789"


def mcp_conn():
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(DSN)
    info.update(user="dip_mcp", password=PASSWORD)
    return psycopg.connect(**info)


@pytest.fixture(scope="module")
def reader(module_warehouse, tmp_path_factory):
    wh = module_warehouse
    with wh.conn.cursor() as cur:
        cur.execute(ROLES_SQL.read_text(encoding="utf-8"))
        cur.execute(f"alter role dip_mcp password '{PASSWORD}'")
    wh.conn.commit()
    wh.init()  # the role exists now: its grants apply
    store = LocalStore(tmp_path_factory.mktemp("store"))
    for name in ("good/elig_good.csv", "bad/elig_bad_rows.csv"):
        store.put(BUCKET_LANDING, f"{FEED}/{Path(name).name}", (FIXTURES / name).read_bytes())
    process_landing(store, wh, load_contracts(), datetime.now(UTC))
    assert run_dbt(DSN, "build", "--select", f"tag:{FEED}", check=False).returncode == 0
    conn = mcp_conn()
    reg = build_registry(conn, store, load_contracts(), ReadServices())
    policy = Policy.load("reader")
    yield lambda tool, **args: reg.call(policy, "test-run", tool, args), conn
    conn.close()


def test_dq_results_latest_per_test(reader):
    call, _ = reader
    got = call("get_dq_results", model="member_eligibility")
    assert got["results"] and {r["model"] for r in got["results"]} == {"member_eligibility"}
    assert all(r["status"] in ("pass", "warn", "error", "fail") for r in got["results"])


def test_profile_file_counts_but_never_shows_phi(reader):
    call, conn = reader
    with conn.cursor() as cur:
        cur.execute("select file_sha256 from ops.intake_file where source_key like %s",
                    ("%elig_bad_rows.csv",))  # fmt: skip
        sha = cur.fetchone()[0]
    conn.commit()
    got = call("profile_file", feed=FEED, file_sha256=sha[:12])
    cols = {c["column"]: c for c in got["untrusted_vendor_content"]}
    assert got["rows"] == 20 and got["missing_contract_columns"] == []
    assert cols["first_name"]["phi"] and "top_values" not in cols["first_name"]
    assert "top_values" not in cols["dob"] and "top_values" not in cols["patient_ref"]
    plan = dict(cols["plan_code"]["top_values"])
    assert plan.get("GOLD") == 1  # the vendor's new plan code, visible: not PHI


def test_the_quarantine_tools_work_as_the_mcp_login(reader):
    call, _ = reader
    groups = call("list_quarantine_groups", feed=FEED)["groups"]
    assert [g["reason_code"] for g in groups] == ["VALUE.NOT_ALLOWED:plan_code"]
    sample = call("get_quarantine_sample", group_id=groups[0]["group_id"])
    assert sample["untrusted_vendor_content"][0]["record"]["plan_code"] == "GOLD"


@pytest.mark.parametrize("sql", [
    "select count(*) from bronze.csv_record",
    "select count(*) from gold.member_eligibility",
    "select count(*) from silver.eligibility_record",
    "select count(*) from ops.approvals",
    "select count(*) from ops.audit_log",
    "delete from ops.quarantine",
    "update ops.audit_log set decision = 'allowed'",
])  # fmt: skip
def test_the_mcp_login_reads_nothing_else_and_cannot_rewrite_the_audit(reader, sql):
    import psycopg

    _, conn = reader
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
        cur.execute(sql)
    conn.rollback()


def test_every_call_is_audited(module_warehouse, reader):
    call, _ = reader
    call("get_feed_history", feed=FEED)
    with module_warehouse.conn.cursor() as cur:
        cur.execute("select count(*) from ops.audit_log where principal = 'reader'")
        assert cur.fetchone()[0] >= 1
    module_warehouse.conn.commit()
