"""Stage 4 (S4.2, S4.3): synthetic 837P and 835 X12 -> bronze -> silver -> the claims and
remittance marts with dbt, on files generated from the invented claims fixture."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

_spec = importlib.util.spec_from_file_location(
    "x12_gen", REPO_ROOT / "packs/healthcare/generators/x12.py"
)
x12 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(x12)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AT = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)


def build(tag: str) -> None:
    assert run_dbt(DSN, "build", "--select", f"tag:{tag}", check=False).returncode == 0


def land(store, warehouse, feed: str, name: str, data: bytes):
    store.put(BUCKET_LANDING, f"{feed}/{name}", data)
    return process_landing(store, warehouse, load_contracts(), AT)


def test_claims_and_remittance_reach_the_marts_and_balance(warehouse, tmp_path):
    claims = x12.read_claims(FIXTURES / "claims/good/claims_good.csv")
    store = LocalStore(tmp_path)
    [o837] = land(
        store,
        warehouse,
        "healthcare.claims_837",
        "claims837_20261021.x12",
        x12.x12_837(claims, "20261021"),
    )
    assert (o837.status, o837.accepted) == ("loaded", 5)
    build("healthcare.claims_837")
    # Before any 835: every line is billed and not yet remitted.
    assert (
        scalar(
            warehouse,
            "select count(*) from gold.claim_payment where payment_status = 'not_remitted'",
        )
        == 5
    )

    # The 835 pays three of the four claims (the fourth, a vaccine, is left for later).
    remitted = [r for r in claims if r["procedure_system"] != "CVX"]
    [o835] = land(
        store,
        warehouse,
        "healthcare.remittance_835",
        "remit835_20261021.x12",
        x12.x12_835(remitted, "20261021"),
    )
    assert (o835.status, o835.accepted) == ("loaded", 4)
    build("healthcare.remittance_835")

    assert rows(
        warehouse,
        "select payment_status, count(*) from gold.claim_payment group by 1 order by 1",
    ) == [("not_remitted", 1), ("paid_adjusted", 4)]
    assert rows(
        warehouse,
        "select charge_amount, paid_amount, contractual_adjustment, patient_responsibility,"
        " days_to_payment from gold.claim_payment where claim_line_id like '%0002-1'",
    ) == [(Decimal("1800.00"), Decimal("960.00"), Decimal("600.00"), Decimal("240.00"), 498)]
    # Paid totals agree with the source claims file, line for line.
    assert scalar(warehouse, "select sum(paid_amount) from gold.remittance_line") == sum(
        Decimal(r["paid_amount"]) for r in remitted
    )
    assert rows(
        warehouse,
        "select distinct test_name, status, failures from ops.dq_results"  # both builds ran them
        " where test_name like 'x12_%' order by 1",
    ) == [
        ("x12_claim_total_matches_lines", "pass", 0),
        ("x12_remit_balances", "pass", 0),
        ("x12_remit_charge_matches_billed", "pass", 0),
        ("x12_remit_line_was_billed", "pass", 0),
    ]


def test_a_broken_envelope_quarantines_the_file_and_a_bad_line_only_itself(warehouse, tmp_path):
    claims = x12.read_claims(FIXTURES / "claims/good/claims_good.csv")
    store = LocalStore(tmp_path)
    good = x12.x12_837(claims, "20261021").decode()
    # One segment lost in transit: SE01 no longer matches the transaction.
    broken = good.replace("PER*IC*SYNTHETIC DATA DESK*TE*5550100000~\n", "", 1)
    [o] = land(store, warehouse, "healthcare.claims_837", "claims837_20261020.x12", broken.encode())
    assert (o.status, o.reason_codes) == ("quarantined", ("X12.ENVELOPE:SE01",))

    # A remittance sent to the claims feed.
    [o] = land(
        store,
        warehouse,
        "healthcare.claims_837",
        "claims837_20261019.x12",
        x12.x12_835(claims, "20261019"),
    )
    assert (o.status, o.reason_codes) == ("quarantined", ("X12.TRANSACTION:835",))

    # One line in five with a member id the contract does not allow: over the contract's
    # max_bad_row_ratio (0.02), so the file is quarantined whole, the line with its code.
    bad = [dict(r) for r in claims]
    bad[0]["member_id"] = "X1"
    [o] = land(
        store,
        warehouse,
        "healthcare.claims_837",
        "claims837_20261018.x12",
        x12.x12_837(bad, "20261018"),
    )
    assert o.status == "quarantined"
    assert set(o.reason_codes) == {"FILE.BAD_ROW_RATIO", "VALUE.PATTERN:member_id"}
    build("healthcare.claims_837")
    assert scalar(warehouse, "select count(*) from gold.professional_claim_line") == 0


def test_a_replacement_claim_replaces_the_whole_claim_and_a_void_removes_it(warehouse, tmp_path):
    claims = x12.read_claims(FIXTURES / "claims/good/claims_good.csv")
    store = LocalStore(tmp_path)
    land(store, warehouse, "healthcare.claims_837", "claims837_20261019.x12",
         x12.x12_837(claims, "20261019"))  # fmt: skip
    # The next day's file replaces claim ...0001 without its second line (frequency 7)
    # and voids claim ...0002 (frequency 8).
    two_line = [r for r in claims if r["claim_id"].endswith("0001")]
    voided = [r for r in claims if r["claim_id"].endswith("0002")]
    text = x12.x12_837([two_line[0], *voided], "20261020").decode()
    text = text.replace("*200.00***11:B:1*", "*200.00***11:B:7*")
    text = text.replace("*1800.00***21:B:1*", "*1800.00***21:B:8*")
    [o] = land(store, warehouse, "healthcare.claims_837", "claims837_20261020.x12", text.encode())
    assert o.status == "loaded"
    build("healthcare.claims_837")
    assert rows(
        warehouse,
        "select right(claim_line_id, 6), claim_frequency from gold.professional_claim_line"
        " order by 1",
    ) == [("0001-1", "7"), ("0003-1", "1"), ("0004-1", "1")]
    assert scalar(warehouse, "select count(*) from ops.dq_results where status <> 'pass'"
                             " and test_name like 'x12_%'") == 0  # fmt: skip
