"""Generate vendor invoices as PDFs, native and scanned (finance pack, Stage 3, S3.1).

The same invoices as the Excel register (invoices.py), as the vendor would send them:
- **native**: a text PDF from reportlab, one invoice per file, in the vendor's layout;
- **scanned**: the native page rendered to an image, then degraded like a paper scan
  (a small rotation, blur and speckle noise) and saved as an image-only PDF, so the only
  way to read it is OCR. `quality="poor"` degrades it until OCR cannot read it reliably,
  for the OCR.LOW_CONFIDENCE path.

Two layouts, chosen per vendor, so the reader cannot rely on one position: `stacked`
(label: value lines) and `table` (a two-column grid). Every PDF says which layout it uses
nowhere: the reader finds the fields by their labels, whatever the layout.

Deterministic: reportlab runs in invariant mode (no timestamps or random ids), the scan's
noise comes from a seeded generator, and the image PDF's dates are fixed, so the same
invoice gives the same bytes. All output is generated and invented.

Usage: python packs/finance/generators/invoice_pdfs.py OUT_DIR [--date YYYYMMDD] [--rows N]
           [--seed S] [--scanned K] [--poor K]
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import random
from datetime import datetime
from decimal import Decimal
from pathlib import Path

LAYOUTS = ("stacked", "table")
FIXED = datetime(2026, 1, 1)


def layout_for(vendor_id: str) -> str:
    return LAYOUTS[int(vendor_id[1:]) % len(LAYOUTS)]


def _money(v: Decimal) -> str:
    return f"{v:,.2f}"


def native_pdf(inv: dict) -> bytes:
    import reportlab.rl_config as rl
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
    from reportlab.rl_config import _reset

    rl.invariant = 1
    _reset()
    rl.invariant = 1
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, invariant=1)
    c.setTitle(f"Invoice {inv['invoice_number']}")
    width, height = LETTER
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, height - 80, inv["vendor_name"])
    c.setFont("Helvetica", 11)
    c.drawString(72, height - 100, f"Vendor ID {inv['vendor_id']}")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 140, "INVOICE")
    fields = [
        ("Invoice number", inv["invoice_number"]),
        ("Invoice date", inv["invoice_date"].isoformat()),
        ("Due date", inv["due_date"].isoformat()),
        ("PO number", inv["po_number"] or ""),
        ("Payment terms", inv["payment_terms"]),
        ("Currency", inv["currency"]),
        ("Subtotal", _money(inv["subtotal"])),
        ("Tax", _money(inv["tax"])),
        ("Total", _money(inv["total"])),
    ]
    c.setFont("Helvetica", 12)
    y = height - 180
    if layout_for(inv["vendor_id"]) == "stacked":
        for label, value in fields:
            c.drawString(72, y, f"{label}: {value}")
            y -= 22
    else:
        for label, value in fields:
            c.rect(72, y - 6, 400, 20)
            c.drawString(80, y, label)
            c.drawRightString(464, y, value)
            y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


def scanned_pdf(native: bytes, seed: int, quality: str = "good") -> bytes:
    import pdfplumber
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    with pdfplumber.open(io.BytesIO(native)) as pdf:
        page = pdf.pages[0].to_image(resolution=200 if quality == "good" else 70).original
    img = page.convert("L")
    angle = rng.uniform(-1.2, 1.2) if quality == "good" else rng.uniform(-4, 4)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(0.6 if quality == "good" else 1.6))
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * (0.002 if quality == "good" else 0.06))):
        px[rng.randrange(w), rng.randrange(h)] = rng.choice((0, 255))
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=200 if quality == "good" else 70,
             creationDate=FIXED.timetuple(), modDate=FIXED.timetuple(), title="scan")  # fmt: skip
    return out.getvalue()


def _register_generator():
    path = Path(__file__).with_name("invoices.py")
    spec = importlib.util.spec_from_file_location("invoices_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--rows", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--scanned", type=int, default=2, help="how many as good scans")
    parser.add_argument("--poor", type=int, default=1, help="how many as unreadable scans")
    args = parser.parse_args()
    gen = _register_generator()
    day = datetime.strptime(args.date, "%Y%m%d").date()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for i, inv in enumerate(gen.invoice_rows(day, args.rows, args.seed)):
        native = native_pdf(inv)
        if i < args.poor:
            data, kind = scanned_pdf(native, args.seed + i, "poor"), "scan-poor"
        elif i < args.poor + args.scanned:
            data, kind = scanned_pdf(native, args.seed + i), "scan"
        else:
            data, kind = native, "native"
        name = f"invoice_{inv['invoice_number']}_{args.date}.pdf"
        (args.out_dir / name).write_bytes(data)
        print(f"wrote {name} ({kind}, {layout_for(inv['vendor_id'])} layout)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
