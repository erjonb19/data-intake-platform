"""Stage 2 (S2.4): claims CSV -> bronze -> silver -> gold.claim_line with dbt, checked
against eligibility, on invented fixtures and (when DIP_SYNTHEA_DIR is set) on the claims
derived from the Synthea bundles."""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dip import BUCKET_LANDING
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AT = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)


def build(tag: str) -> None:
    assert run_dbt(DSN, "build", "--select", f"tag:{tag}", check=False).returncode == 0


def land(store, warehouse, feed: str, name: str, data: bytes):
    store.put(BUCKET_LANDING, f"{feed}/{name}", data)
    return process_landing(store, warehouse, load_contracts(), AT)


def test_claims_reach_gold_and_coverage_findings_are_warnings(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    land(store, warehouse, "healthcare.eligibility", "elig_good.csv",
         (FIXTURES / "eligibility/good/elig_good.csv").read_bytes())  # fmt: skip
    build("healthcare.eligibility")
    land(store, warehouse, "healthcare.claims", "claims_good.csv",
         (FIXTURES / "claims/good/claims_good.csv").read_bytes())  # fmt: skip
    [bad] = land(store, warehouse, "healthcare.claims", "claims_missing_paid_amount.csv",
                 (FIXTURES / "claims/bad/claims_missing_paid_amount.csv").read_bytes())  # fmt: skip
    assert bad.reason_codes == ("SCHEMA.MISSING_COLUMN:paid_amount",)
    build("healthcare.claims")

    assert scalar(warehouse, "select count(*) from gold.claim_line") == 5
    assert rows(
        warehouse,
        "select member_id, service_date::text, paid_amount, procedure_system"
        " from gold.claim_line where claim_line_id like '%0002-1'",
    ) == [("M000000003", "2025-06-10", Decimal("960.00"), "SNOMED")]
    assert rows(
        warehouse,
        "select test_name, status, failures from ops.dq_results"
        " where test_name like 'claims_%' order by 1",
    ) == [("claims_amounts_ordered", "pass", 0), ("claims_within_coverage", "warn", 2)]


def test_a_corrected_resend_replaces_the_line(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    good = (FIXTURES / "claims/good/claims_good.csv").read_bytes()
    land(store, warehouse, "healthcare.claims", "claims_20261019.csv", good)
    land(store, warehouse, "healthcare.claims", "claims_20261020.csv",
         good.replace(b"960.00,140.00,100.00", b"900.00,200.00,100.00"))  # fmt: skip
    build("healthcare.claims")
    assert rows(
        warehouse,
        "select paid_amount, file_date::text from gold.claim_line"
        " where claim_line_id like '%0002-1'",
    ) == [(Decimal("900.00"), "2026-10-20")]


def _generator():
    path = REPO_ROOT / "packs/healthcare/generators/claims.py"
    spec = importlib.util.spec_from_file_location("claims_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not os.environ.get("DIP_SYNTHEA_DIR"), reason="DIP_SYNTHEA_DIR not set")
def test_synthea_claims_load_whole(warehouse, tmp_path):
    """Every claim line derived from the Synthea bundles passes the contract and the
    amount checks."""
    data = _generator().claims_csv(Path(os.environ["DIP_SYNTHEA_DIR"]))
    store = LocalStore(tmp_path)
    [o] = land(store, warehouse, "healthcare.claims", "claims_20261021.csv", data)
    assert (o.status, o.rejected) == ("loaded", 0), o
    build("healthcare.claims")
    assert scalar(warehouse, "select count(*) from gold.claim_line") == o.accepted
    assert (
        scalar(
            warehouse,
            "select status from ops.dq_results where test_name = 'claims_amounts_ordered'"
            " order by recorded_at desc limit 1",
        )
        == "pass"
    )
