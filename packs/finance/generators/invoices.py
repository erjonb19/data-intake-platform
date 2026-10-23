"""Generate a synthetic AP invoice register as an Excel workbook (finance pack, S2.5).

One row per invoice in the layout of the `finance.vendor_invoice` contract: sheet
`Invoices`, a title block on rows 1-2, the header on row 3. Vendors, POs and amounts are
invented by a seeded random generator (the standard library's, no Faker), so the same seed
and date always give a byte-identical file: the workbook's own timestamps and the zip
entries' dates are fixed too.

  invoice_number   INV-<6 digits>, unique within and across days for one seed
  vendor_id/name   one of 12 invented vendors
  invoice_date     within the 5 days before the file date
  due_date         invoice_date + the payment terms (NET15/30/45/60)
  po_number        PO-<6 digits>
  subtotal, tax    tax is 8.875% of the subtotal, rounded to the cent; total = subtotal + tax

Breakages (`--breakage`, one row each, for the E2E and later the Stage 4 drills):
  duplicate        a second row with an existing invoice number (KEY.DUPLICATE)
  negative_total   a credit entered as a negative total (VALUE.OUT_OF_RANGE)
  missing_po       an invoice without its PO number (VALUE.REQUIRED)
  total_mismatch   a total that is not subtotal + tax (passes the contract; the dbt DQ
                   test invoice_totals_add_up finds it)

All output is generated and invented.

Usage: python packs/finance/generators/invoices.py OUT_DIR [--date YYYYMMDD] [--rows N]
           [--seed S] [--breakage NAME ...]
"""

from __future__ import annotations

import argparse
import io
import random
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

COLUMNS = ("invoice_number", "vendor_id", "vendor_name", "invoice_date", "due_date",
           "po_number", "payment_terms", "currency", "subtotal", "tax", "total")  # fmt: skip
VENDORS = [
    ("V1001", "Northwind Office Supply"),
    ("V1002", "Blue Harbor Logistics"),
    ("V1003", "Granite Peak Consulting"),
    ("V1004", "Maple Lane Facilities"),
    ("V1005", "Silverline IT Services"),
    ("V1006", "Cedar Street Printing"),
    ("V1007", "Brightwater Cleaning Co"),
    ("V1008", "Ironwood Equipment Rental"),
    ("V1009", "Redfern Catering"),
    ("V1010", "Summit Legal Partners"),
    ("V1011", "Oakridge Security"),
    ("V1012", "Lakeshore Telecom"),
]
TERMS = {"NET15": 15, "NET30": 30, "NET45": 45, "NET60": 60}
TAX_RATE = Decimal("0.08875")
BREAKAGES = ("duplicate", "negative_total", "missing_po", "total_mismatch")
FIXED = datetime(2026, 1, 1, 0, 0, 0)
CENT = Decimal("0.01")


def invoice_rows(file_date: date, rows: int, seed: int) -> list[dict]:
    # One stream per (seed, date), so each day's numbers differ but are reproducible.
    rng = random.Random(f"{seed}-{file_date.isoformat()}")
    day_base = (file_date - date(2020, 1, 1)).days * 1000
    out = []
    for i in range(rows):
        vendor_id, vendor_name = rng.choice(VENDORS)
        terms = rng.choice(list(TERMS))
        invoice_date = file_date - timedelta(days=rng.randint(0, 5))
        subtotal = (Decimal(rng.randint(5_000, 2_500_000)) / 100).quantize(CENT)
        tax = (subtotal * TAX_RATE).quantize(CENT, ROUND_HALF_UP)
        out.append(
            {
                "invoice_number": f"INV-{(day_base + i) % 10**6:06d}",
                "vendor_id": vendor_id,
                "vendor_name": vendor_name,
                "invoice_date": invoice_date,
                "due_date": invoice_date + timedelta(days=TERMS[terms]),
                "po_number": f"PO-{rng.randint(0, 999_999):06d}",
                "payment_terms": terms,
                "currency": "USD",
                "subtotal": subtotal,
                "tax": tax,
                "total": subtotal + tax,
            }
        )
    return out


def apply_breakages(rows: list[dict], breakages: list[str]) -> list[dict]:
    rows = [dict(r) for r in rows]
    for b in breakages:
        if b == "duplicate":
            rows.append({**rows[1], "po_number": "PO-999999"})
        elif b == "negative_total":
            rows[2]["total"] = -rows[2]["total"]
        elif b == "missing_po":
            rows[3]["po_number"] = None
        elif b == "total_mismatch":
            rows[4]["total"] = rows[4]["total"] + Decimal("10.00")
        else:
            raise ValueError(f"unknown breakage {b!r}; known: {BREAKAGES}")
    return rows


def _deterministic(data: bytes) -> bytes:
    """Rewrite the .xlsx zip with fixed entry dates, and the `modified` time openpyxl stamps
    at save, so the bytes depend on content only."""
    out = io.BytesIO()
    stamp = FIXED.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in sorted(src.infolist(), key=lambda i: i.filename):
            body = src.read(item.filename)
            if item.filename == "docProps/core.xml":
                body = re.sub(
                    rb"(<dcterms:modified[^>]*>)[^<]*(<)", rb"\g<1>" + stamp + rb"\2", body
                )
            info = zipfile.ZipInfo(item.filename, date_time=FIXED.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            dst.writestr(info, body)
    return out.getvalue()


def workbook_bytes(rows: list[dict], file_date: date) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.properties.creator = "dip invoice generator"
    wb.properties.created = FIXED
    wb.properties.modified = FIXED
    ws = wb.active
    ws.title = "Invoices"
    ws.append([f"Invoice register for {file_date.isoformat()} (generated, invented data)"])
    ws.append([])
    ws.append(list(COLUMNS))
    for r in rows:
        ws.append([float(r[c]) if isinstance(r[c], Decimal) else r[c] for c in COLUMNS])
    for row in ws.iter_rows(min_row=4):
        for cell, col in zip(row, COLUMNS, strict=True):
            if col.endswith("_date"):
                cell.number_format = "yyyy-mm-dd"
            elif col in ("subtotal", "tax", "total"):
                cell.number_format = "#,##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    return _deterministic(buf.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--date", default="20260101", help="file date YYYYMMDD")
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--breakage", action="append", default=[], choices=BREAKAGES)
    args = parser.parse_args()
    file_date = datetime.strptime(args.date, "%Y%m%d").date()
    rows = apply_breakages(invoice_rows(file_date, args.rows, args.seed), args.breakage)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"invoices_{args.date}.xlsx"
    out.write_bytes(workbook_bytes(rows, file_date))
    print(f"wrote {out}: {len(rows)} invoices, breakages {args.breakage or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
