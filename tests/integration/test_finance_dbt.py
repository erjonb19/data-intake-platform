"""Stage 2 (S2.5): the finance pack's Excel invoice registers -> bronze -> silver -> gold
with dbt, on the generated, invented fixtures."""

from __future__ import annotations

import shutil
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
PDF_FEED = "finance.vendor_invoice_pdf"
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


def test_invoice_pdfs_are_matched_to_the_register(warehouse, tmp_path):
    """Stage 3 (S3.1): invoice PDFs -> silver -> gold.invoice_match against the Excel
    register. The native PDFs need no OCR; the scan is added where Tesseract is installed."""
    store = LocalStore(tmp_path)
    store.put(BUCKET_LANDING, f"{FEED}/invoices_good.xlsx",
              (FIXTURES / "good/invoices_good.xlsx").read_bytes())  # fmt: skip
    pdfs = ["invoice_native_stacked.pdf", "invoice_native_table.pdf",
            "invoice_total_differs.pdf", "invoice_not_in_register.pdf"]  # fmt: skip
    if shutil.which("tesseract"):
        pdfs.append("invoice_scan.pdf")
    for name in pdfs:
        store.put(BUCKET_LANDING, f"{PDF_FEED}/{name}", (FIXTURES / "good" / name).read_bytes())
    store.put(BUCKET_LANDING, f"{PDF_FEED}/invoice_not_a_pdf.pdf",
              (FIXTURES / "bad/invoice_not_a_pdf.pdf").read_bytes())  # fmt: skip
    outcomes = {o.source_key.split("/")[-1]: o for o in process_landing(
        store, warehouse, load_contracts(), AT)}  # fmt: skip
    assert outcomes["invoice_not_a_pdf.pdf"].reason_codes == ("PDF.INVALID",)
    assert all(outcomes[n].accepted == 1 for n in pdfs)
    for tag in (FEED, PDF_FEED):
        assert run_dbt(DSN, "build", "--select", f"tag:{tag}", check=False).returncode == 0

    statuses = dict(rows(warehouse, "select match_status, count(*)::int from gold.invoice_match"
                                    " group by 1"))  # fmt: skip
    assert statuses == {"matched": len(pdfs) - 2, "total_differs": 1, "not_in_register": 1}
    assert rows(
        warehouse,
        "select pdf_total - register_total from gold.invoice_match"
        " where match_status = 'total_differs'",
    ) == [(Decimal("10.00"),)]
    # The mismatches are a finding for AP (a warning), not a failed build.
    assert rows(
        warehouse,
        "select status, failures from ops.dq_results"
        " where test_name = 'invoice_pdf_matches_register'",
    ) == [("warn", 2)]
