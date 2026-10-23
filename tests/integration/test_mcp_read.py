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


def test_profile_file_shows_no_values_of_an_unknown_column(module_warehouse, reader, tmp_path):
    # A vendor renames dob to " DOB": the contract has no DOB, so it may be PHI. Counts only.
    _, conn = reader
    lines = (FIXTURES / "good/elig_good.csv").read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("dob", " DOB")
    store = LocalStore(tmp_path)
    data = ("\n".join(lines) + "\n").encode()
    store.put(BUCKET_LANDING, f"{FEED}/elig_renamed.csv", data)
    [o] = process_landing(store, module_warehouse, load_contracts(), datetime.now(UTC))
    reg = build_registry(conn, store, load_contracts(), ReadServices())
    got = reg.call(Policy.load("reader"), "t", "profile_file",
                   {"feed": FEED, "file_sha256": o.file_sha256[:12]})  # fmt: skip
    cols = {c["column"]: c for c in got["untrusted_vendor_content"]}
    assert "DOB" in cols and not cols["DOB"]["in_contract"] and "top_values" not in cols["DOB"]
    assert got["missing_contract_columns"] == ["dob"]


def test_the_quarantine_tools_work_as_the_mcp_login(reader):
    call, _ = reader
    groups = {g["reason_code"]: g for g in call("list_quarantine_groups", feed=FEED)["groups"]}
    group = groups["VALUE.NOT_ALLOWED:plan_code"]
    sample = call("get_quarantine_sample", group_id=group["group_id"])
    record = sample["untrusted_vendor_content"][0]["record"]
    # The reader sees the bad value (not PHI) but never a PHI column's value.
    assert record["plan_code"] == "GOLD" and record["sex"] in ("F", "M", "U")
    assert {
        record[c]
        for c in ("member_id", "first_name", "last_name", "dob", "patient_ref", "coverage_start")
    } == {"(PHI: not shown)"}


@pytest.mark.parametrize("sql", [
    "select count(*) from bronze.csv_record",
    "select count(*) from gold.member_eligibility",
    "select count(*) from silver.eligibility_record",
    "update ops.approvals set decided_by = 'human:x', approved_sha256 = 'x'",
    "update ops.approvals set payload = '{}'::jsonb",
    "delete from ops.approvals",
    "update ops.quarantine set status = 'discarded'",
    "select count(*) from ops.audit_log",
    "delete from ops.quarantine",
    "update ops.audit_log set decision = 'allowed'",
])  # fmt: skip
def test_the_mcp_login_reads_nothing_else_and_cannot_rewrite_the_audit_or_approve(reader, sql):
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


def test_the_cluster_triage_login_completes_only_a_resend(module_warehouse, reader, tmp_path):
    """Stage 6: as dip_mcp, a person-approved discard is refused up front (the login cannot
    write quarantine), so the approval stays approved for a person to run; a resend runs;
    and the login cannot re-arm the executed approval to run it twice."""
    import psycopg

    from dip.agents import approvals
    from dip.agents.registry import Denied

    _, conn = reader
    wh = module_warehouse
    with wh.conn.cursor() as cur:
        cur.execute("select quarantine_id from ops.quarantine where status = 'open' limit 1")
        (qid,) = cur.fetchone()
    wh.conn.commit()
    ids = []
    for action in ("discard", "request_resend"):
        payload = {"group_id": f"{FEED}|x", "feed": FEED, "quarantine_ids": [qid],
                   "message": "resend"}  # fmt: skip
        aid = approvals.create(wh.conn, "triage", None, action, payload)
        approvals.decide(wh.conn, aid, True, "human:test")
        ids.append(aid)
    reg = build_registry(conn, LocalStore(tmp_path), load_contracts(), ReadServices())
    triage = Policy.load("triage")
    with pytest.raises(Denied, match="cannot run a discard"):
        reg.call(triage, "t", "execute_approved_operation", {"approval_id": ids[0]})
    assert reg.call(triage, "t", "execute_approved_operation",
                    {"approval_id": ids[1]})["status"] == "executed"  # fmt: skip
    with conn.cursor() as cur:
        cur.execute("select status from ops.approvals where approval_id = any(%s) order by 1",
                    (ids,))  # fmt: skip
        assert [r[0] for r in cur.fetchall()] == ["approved", "executed"]
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="cannot become"), conn.cursor() as c:
        c.execute("update ops.approvals set status = 'approved' where approval_id = %s",
                  (ids[1],))  # fmt: skip
    conn.rollback()
