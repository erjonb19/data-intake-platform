"""Stage 2 (S2.5): the finance pack's Excel invoice registers -> bronze -> silver -> gold
with dbt, on the generated, invented fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows, scalar

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
FEED = "finance.vendor_invoice"
AT = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)


def test_invoice_registers_reach_gold(warehouse, tmp_path):
    store = LocalStore(tmp_path)
    for name in ("good/invoices_good.xlsx", "bad/invoices_breakages.xlsx",
                 "bad/invoices_sheet_renamed.xlsx"):  # fmt: skip
        store.put(BUCKET_LANDING, f"{FEED}/{Path(name).name}", (FIXTURES / name).read_bytes())
    outcomes = process_landing(store, warehouse, load_contracts(), AT)
    out = {o.source_key.split("/")[-1]: o for o in outcomes}
    assert out["invoices_sheet_renamed.xlsx"].reason_codes == ("SCHEMA.MISSING_SHEET:Invoices",)
    assert (out["invoices_breakages.xlsx"].accepted, out["invoices_breakages.xlsx"].rejected) == (
        37,
        4,
    )
    assert run_dbt(DSN, "build", "--select", f"tag:{FEED}", check=False).returncode == 0

    assert scalar(warehouse, "select count(*) from gold.vendor_invoice") == 12 + 37
    # Amounts round-trip exactly from Excel numbers.
    assert rows(
        warehouse,
        "select subtotal, tax, total, invoice_date::text from gold.vendor_invoice"
        " where invoice_number = 'INV-483000'",
    ) == [(Decimal("931.64"), Decimal("82.68"), Decimal("1014.32"), "2026-10-14")]
    # The wrong total passes the contract and is found by the DQ test, as a warning.
    assert rows(
        warehouse,
        "select test_name, status, failures from ops.dq_results"
        " where test_name like 'invoice_%' order by 1",
    ) == [("invoice_due_after_invoice", "pass", 0), ("invoice_totals_add_up", "warn", 1)]
