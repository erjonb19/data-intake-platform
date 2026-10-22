"""Writes the finance fixtures (python tests/fixtures/finance/make_fixtures.py).

Generated, invented invoice registers from packs/finance/generators/invoices.py with a
fixed seed and date, so rerunning this gives byte-identical files. Names carry no date, so
the E2E can land them on any day (the received date stands in for the business date).
"""

from __future__ import annotations

import importlib.util
import io
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
DAY = date(2026, 10, 19)
# A register of the day before: invoice numbers are per day, so two of one day would clash.
BROKEN_DAY = date(2026, 10, 18)


def _gen():
    path = ROOT / "packs/finance/generators/invoices.py"
    spec = importlib.util.spec_from_file_location("invoices_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    gen = _gen()
    good = gen.invoice_rows(DAY, 12, seed=7)
    broken = gen.apply_breakages(
        gen.invoice_rows(BROKEN_DAY, 40, seed=8),
        ["duplicate", "negative_total", "missing_po", "total_mismatch"],
    )
    files = {
        "good/invoices_good.xlsx": gen.workbook_bytes(good, DAY),
        "bad/invoices_breakages.xlsx": gen.workbook_bytes(broken, BROKEN_DAY),
        "bad/invoices_sheet_renamed.xlsx": _renamed(gen.workbook_bytes(good[:3], DAY), gen),
    }
    files.update(_pdfs(good))
    for name, data in files.items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {name}")


def _pdfs(good: list[dict]) -> dict[str, bytes]:
    """Stage 3: invoice PDFs of the same day's register, so gold.invoice_match has both a
    match and each kind of mismatch to find."""
    path = ROOT / "packs/finance/generators/invoice_pdfs.py"
    spec = importlib.util.spec_from_file_location("invoice_pdfs_gen", path)
    pdfs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pdfs)
    stacked = next(i for i in good if pdfs.layout_for(i["vendor_id"]) == "stacked")
    table = next(i for i in good if pdfs.layout_for(i["vendor_id"]) == "table")
    rest = [i for i in good if i not in (stacked, table)]
    differs = dict(rest[0], total=rest[0]["total"] + 10)  # the vendor billed $10 more
    unknown = dict(rest[1], invoice_number="INV-999999")  # not (yet) in the register
    return {
        "good/invoice_native_stacked.pdf": pdfs.native_pdf(stacked),
        "good/invoice_native_table.pdf": pdfs.native_pdf(table),
        "good/invoice_scan.pdf": pdfs.scanned_pdf(pdfs.native_pdf(rest[2]), seed=11),
        "good/invoice_total_differs.pdf": pdfs.native_pdf(differs),
        "good/invoice_not_in_register.pdf": pdfs.native_pdf(unknown),
        "bad/invoice_scan_poor.pdf": pdfs.scanned_pdf(pdfs.native_pdf(rest[3]), 12, "poor"),
        "bad/invoice_not_a_pdf.pdf": b"%PDF-1.7\nthis is not really a PDF\n",
    }


def _renamed(data: bytes, gen) -> bytes:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data))
    wb.active.title = "Sheet1"
    wb.properties.created = wb.properties.modified = gen.FIXED
    buf = io.BytesIO()
    wb.save(buf)
    return gen._deterministic(buf.getvalue())


if __name__ == "__main__":
    main()
