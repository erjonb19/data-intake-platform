"""Intake jobs (Stage 10, S10.2; dip/intake_jobs.py): the queue, the claim, the onboarding
samples that land under landing/_onboard/, and the drift job an executed
propose_contract_change queues."""

from __future__ import annotations

from datetime import timedelta

from dip import BUCKET_LANDING, BUCKET_RAW
from dip.agents import approvals
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.intake_jobs import claim_one, finish, onboard_target, queue, scan_onboarding
from dip.storage import LocalStore
from tests.integration.conftest import rows

CSV = b"po_number,vendor_id,amount\nPO-1,V1,10.00\n"
MISSING = (
    b"first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref\n"
    b"Alex,Hallett,1980-02-14,F,HMO,2024-01-01,,\n"
)


def test_a_sample_under_onboard_becomes_a_sample_and_a_queued_job(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, "_onboard/finance.purchase_orders/po_20261030.csv", CSV)
    out = scan_onboarding(warehouse.conn, store, load_contracts())
    assert len(out["queued"]) == 1 and not out["refused"]
    (sha,) = rows(warehouse, "select file_sha256 from ops.sample_file"
                             " where feed = 'finance.purchase_orders'")[0]  # fmt: skip
    jobs = rows(warehouse, "select feed, mode, file_sha256, source, status from ops.intake_job")
    assert jobs == [("finance.purchase_orders", "onboard", sha, "landing", "queued")]
    # Moved, not copied: landing is empty and the raw copy is under raw/samples/.
    assert store.list(BUCKET_LANDING, "_onboard/") == []
    assert store.list(BUCKET_RAW, "samples/finance.purchase_orders/")
    # A second sample for the same feed while its job is open: registered, not queued.
    store.put(BUCKET_LANDING, "_onboard/finance.purchase_orders/po_20261031.csv", CSV + b"x\n")
    out = scan_onboarding(warehouse.conn, store, load_contracts())
    assert out["queued"] == [] and len(out["already_open"]) == 1
    assert rows(warehouse, "select count(*) from ops.sample_file") == [(2,)]
    assert rows(warehouse, "select count(*) from ops.intake_job") == [(1,)]


def test_unusable_onboarding_keys_are_left_in_place_and_reported(warehouse, tmp_path):
    contracts = load_contracts()
    store = LocalStore(tmp_path)
    bad = [
        "_onboard/Finance.PO/a.csv",                    # not <pack>.<name> lower case
        "_onboard/nopack.orders/a.csv",                 # no such pack
        "_onboard/healthcare.eligibility/a.csv",        # already has a contract
        "_onboard/a.csv",                               # no feed folder
    ]  # fmt: skip
    for key in bad:
        store.put(BUCKET_LANDING, key, CSV)
    out = scan_onboarding(warehouse.conn, store, contracts)
    assert len(out["refused"]) == 4 and out["queued"] == []
    assert sorted(store.list(BUCKET_LANDING, "_onboard/")) == sorted(bad)
    assert rows(warehouse, "select count(*) from ops.intake_job") == [(0,)]
    assert onboard_target("_onboard/finance.orders/x.csv", contracts) == ("finance.orders", "x.csv")


def test_jobs_are_claimed_one_at_a_time_and_a_dead_pods_claim_is_taken_back(warehouse):
    with warehouse.conn.cursor() as cur:
        a = queue(cur, "finance.a", "onboard", "s" * 64, "landing")
        b = queue(cur, "finance.b", "onboard", "t" * 64, "landing")
        assert queue(cur, "finance.a", "onboard", "u" * 64, "landing") is None  # one open
    warehouse.conn.commit()
    first = claim_one(warehouse.conn, "run-1")
    assert first and first["job_id"] == a and first["mode"] == "onboard"
    assert claim_one(warehouse.conn, "run-1")["job_id"] == b
    assert claim_one(warehouse.conn, "run-1") is None
    # a's pod died 31 minutes ago: its claim is taken back; b's (just now) is not.
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.intake_job set claimed_at = now() - %s where job_id = %s",
                    (timedelta(minutes=31), a))  # fmt: skip
    warehouse.conn.commit()
    assert claim_one(warehouse.conn, "run-2")["job_id"] == a
    finish(warehouse.conn, a, "proposed", chain_id="intake-1", provider="map:anthropic",
           approval_id=7)  # fmt: skip
    got = rows(warehouse, "select status, chain_id, provider, approval_id from ops.intake_job"
                          " where job_id = %s", (a,))  # fmt: skip
    assert got == [("proposed", "intake-1", "map:anthropic", 7)]
    # Finished, the feed can be queued again.
    with warehouse.conn.cursor() as cur:
        assert queue(cur, "finance.a", "drift", "v" * 64, "handoff") is not None
    warehouse.conn.commit()


def test_an_executed_propose_contract_change_queues_a_drift_job(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, "healthcare.eligibility/elig_20261021.csv", MISSING)
    process_landing(store, warehouse, load_contracts())
    ids, sha = zip(*rows(warehouse, "select quarantine_id, file_sha256 from ops.quarantine"
                                    " where status = 'open'"), strict=True)  # fmt: skip
    group = "healthcare.eligibility|SCHEMA.MISSING_COLUMN:member_id"
    aid = approvals.create(warehouse.conn, "triage", "run-1", "propose_contract_change",
                           {"group_id": group, "quarantine_ids": list(ids),
                            "root_cause": "schema_drift", "confidence": 0.9,
                            "rationale": "the vendor renamed member_id"})  # fmt: skip
    approvals.decide(warehouse.conn, aid, True, "human:alice@test")
    result = approvals.execute(warehouse.conn, store, aid, "human:alice@test")
    assert result["handoff"] == "intake team (drift)" and result["job_id"]
    got = rows(warehouse, "select feed, mode, file_sha256, source, source_ref, status"
                          " from ops.intake_job")  # fmt: skip
    assert got == [("healthcare.eligibility", "drift", sha[0], "handoff", f"approval:{aid}",
                    "queued")]  # fmt: skip
    # No data changed: the quarantine stays open for the contract change and its replay.
    assert rows(warehouse, "select distinct status from ops.quarantine") == [("open",)]


def test_a_resent_sample_is_not_queued_again_and_one_bad_key_blocks_nothing(
    warehouse, tmp_path, monkeypatch
):
    from dip import intake_jobs

    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, "_onboard/finance.purchase_orders/po_20261030.csv", CSV)
    scan_onboarding(warehouse.conn, store, load_contracts())
    (job,) = rows(warehouse, "select job_id from ops.intake_job")[0]
    with warehouse.conn.cursor() as cur:  # the job ran and ended
        cur.execute("update ops.intake_job set status = 'running' where job_id = %s", (job,))
    warehouse.conn.commit()
    assert finish(warehouse.conn, job, "no_change")
    # The same content again (a resend, or a delete that failed): registered, not queued.
    store.put(BUCKET_LANDING, "_onboard/finance.purchase_orders/po_20261031.csv", CSV)
    # And a file too big for a sample, before it in key order: refused, the other goes on.
    store.put(BUCKET_LANDING, "_onboard/finance.aaa_orders/big.csv", b"x" * 64)
    monkeypatch.setattr(intake_jobs, "MAX_SAMPLE_BYTES", 50)
    out = scan_onboarding(warehouse.conn, store, load_contracts())
    assert len(out["already_done"]) == 1 and out["queued"] == [], out
    assert any("over 50" in r for r in out["refused"])
    assert rows(warehouse, "select count(*) from ops.intake_job") == [(1,)]
    assert store.list(BUCKET_LANDING, "_onboard/") == ["_onboard/finance.aaa_orders/big.csv"]
    # process_landing (dip intake) skips the samples left under _onboard/.
    assert process_landing(store, warehouse, load_contracts()) == []


def test_finish_only_ends_the_claim_it_was_given(warehouse):
    with warehouse.conn.cursor() as cur:
        a = queue(cur, "finance.a", "onboard", "s" * 64, "landing")
    warehouse.conn.commit()
    assert not finish(warehouse.conn, a, "failed")  # still queued: nothing to end
    claim_one(warehouse.conn, "run-1")
    with warehouse.conn.cursor() as cur:  # run-1's pod died; run-2 takes the claim back
        cur.execute("update ops.intake_job set claimed_at = now() - %s", (timedelta(minutes=31),))
    warehouse.conn.commit()
    claim_one(warehouse.conn, "run-2")
    assert not finish(warehouse.conn, a, "failed", dag_run_id="run-1")  # the slow pod
    assert finish(warehouse.conn, a, "proposed", dag_run_id="run-2")
    assert rows(warehouse, "select status from ops.intake_job") == [("proposed",)]
