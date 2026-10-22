"""PDF invoices (Stage 3, S3.1): dip/pdffeed.py reads native PDFs by their text layer and
scans by OCR, in both vendor layouts, and gives the CSV engine's codes for a misread value.
The fixtures are generated and invented (tests/fixtures/finance/make_fixtures.py). The OCR
tests need Tesseract, which only the OCR task image and CI have: they skip without it."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dip.contracts import load_contracts
from dip.pdffeed import _value, validate_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="Tesseract is only in the OCR image and on CI"
)


@pytest.fixture(scope="module")
def contract():
    return load_contracts()["finance.vendor_invoice_pdf"]


def read(contract, name: str, data: bytes | None = None):
    return validate_pdf(data or (FIXTURES / name).read_bytes(), contract, received_at=NOW)


def record(result) -> dict:
    assert not result.file_violations, result.file_violations
    assert len(result.accepted) == 1, result.rejected
    return result.accepted[0].record


@pytest.mark.parametrize(
    "name", ["good/invoice_native_stacked.pdf", "good/invoice_native_table.pdf"]
)
def test_native_pdfs_are_read_in_either_layout(contract, name):
    rec = record(read(contract, name))
    assert rec["extraction"] == "native" and rec["ocr_confidence"] == ""
    assert rec["invoice_number"].startswith("INV-") and rec["vendor_id"].startswith("V")
    # Amounts lose the thousands separators the vendor printed; they still add up.
    assert Decimal(rec["subtotal"]) + Decimal(rec["tax"]) == Decimal(rec["total"])


def test_letterhead_is_the_vendor_name(contract):
    rec = record(read(contract, "good/invoice_native_stacked.pdf"))
    assert rec["vendor_name"] and not rec["vendor_name"].startswith("Vendor ID")


def test_not_a_pdf_is_quarantined(contract):
    result = read(contract, "bad/invoice_not_a_pdf.pdf")
    assert [v.reason_code for v in result.file_violations] == ["PDF.INVALID"]


def test_labels_are_found_with_or_without_a_colon():
    text = "Acme Supplies\nInvoice number: INV-000001\nSubtotal      10.00\n"
    assert _value(text, "Invoice number") == "INV-000001"
    assert _value(text, "Subtotal") == "10.00"
    assert _value(text, "Total") is None


def test_a_missing_label_names_the_field(contract, monkeypatch):
    # No total line: PDF.MISSING_FIELD:total, not a guessed total.
    import dip.pdffeed as pdffeed

    native = (FIXTURES / "good/invoice_native_stacked.pdf").read_bytes()
    text, conf = pdffeed.read_text(native, contract.spec["format"], pdffeed.CsvResult())
    cut = "\n".join(ln for ln in text.splitlines() if not ln.startswith("Total"))
    monkeypatch.setattr(pdffeed, "read_text", lambda *a: (cut, conf))
    result = validate_pdf(native, contract, received_at=NOW)
    assert [v.reason_code for v in result.file_violations] == ["PDF.MISSING_FIELD:total"]


def test_a_misread_value_gets_the_csv_code(contract, monkeypatch):
    # A scan that reads "INV-4830O0" (O for 0) fails the pattern like any other format.
    import dip.pdffeed as pdffeed

    native = (FIXTURES / "good/invoice_native_stacked.pdf").read_bytes()
    text, conf = pdffeed.read_text(native, contract.spec["format"], pdffeed.CsvResult())
    bad = text.replace(record(read(contract, "good/invoice_native_stacked.pdf"))["invoice_number"],
                       "INV-4830O0")  # fmt: skip
    monkeypatch.setattr(pdffeed, "read_text", lambda *a: (bad, conf))
    result = validate_pdf(native, contract, received_at=NOW)
    assert not result.accepted
    assert result.rejected[0][1].reason_code == "VALUE.PATTERN:invoice_number"


def test_an_image_only_pdf_without_tesseract_says_so(contract, monkeypatch):
    import pytesseract

    def missing(*_, **__):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", missing)
    result = read(contract, "good/invoice_scan.pdf")
    assert [v.reason_code for v in result.file_violations] == ["OCR.UNAVAILABLE"]


def test_a_task_without_the_pdf_reader_says_so(contract, monkeypatch):
    # Not in the OCR image: even a native PDF is refused with a code, never a crash.
    import builtins

    real = builtins.__import__

    def no_pdfplumber(name, *a, **k):
        if name == "pdfplumber":
            raise ImportError("No module named 'pdfplumber'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pdfplumber)
    result = read(contract, "good/invoice_native_table.pdf")
    assert [v.reason_code for v in result.file_violations] == ["OCR.UNAVAILABLE"]


def _pdf(pages: int = 1, blank_after: bool = False, size=(612, 792)) -> bytes:
    """A small native invoice, from the fixture's text, built here."""
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size, invariant=1)
    for _ in range(pages):
        y = size[1] - 72
        for line in ["Acme Supplies", "Vendor ID V1001", "Invoice number: INV-000001",
                     "Invoice date: 2026-10-14", "Due date: 2026-11-13", "PO number: PO-000001",
                     "Payment terms: NET30", "Currency: USD", "Subtotal: 1,000.00",
                     "Tax: 88.75", "Total: 1,088.75"]:  # fmt: skip
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    if blank_after:
        c.showPage()
    c.save()
    return buf.getvalue()


def test_a_blank_trailing_page_keeps_a_typed_invoice_native(contract):
    rec = record(read(contract, "", _pdf(blank_after=True)))
    assert rec["extraction"] == "native" and rec["total"] == "1088.75"


def test_too_many_pages_is_refused_before_reading_them(contract):
    result = read(contract, "", _pdf(pages=25))
    assert [v.reason_code for v in result.file_violations] == ["PDF.TOO_MANY_PAGES"]


def test_a_huge_scanned_page_is_refused_before_rendering(contract):
    import io

    from PIL import Image

    buf = io.BytesIO()
    # A 100 x 100 inch "scan": 9e8 px at 300 dpi, refused before rendering.
    Image.new("L", (300, 300), 255).save(buf, format="PDF", resolution=3)
    result = read(contract, "", buf.getvalue())
    assert [v.reason_code for v in result.file_violations] == ["PDF.PAGE_TOO_LARGE"]


def test_a_parser_failure_anywhere_is_pdf_invalid(contract):
    # A valid header and a broken body: pdfminer fails while reading pages, not on open.
    data = _pdf()
    broken = data[: len(data) // 2] + b"garbage" * 50
    result = read(contract, "", broken)
    assert [v.reason_code for v in result.file_violations] == ["PDF.INVALID"]


def test_an_oversized_file_is_refused(contract, monkeypatch):
    import dip.pdffeed as pdffeed

    monkeypatch.setattr(pdffeed, "MAX_BYTES", 100)
    result = read(contract, "good/invoice_native_table.pdf")
    assert [v.reason_code for v in result.file_violations] == ["FILE.TOO_LARGE"]


def test_a_longer_label_is_not_the_value():
    text = "Tax ID: 99-1234567\nTax: 88.75\nTotal 1,088.75\n"
    assert _value(text, "Tax") == "88.75"
    assert _value(text, "Total") == "1,088.75"


@needs_tesseract
def test_a_good_scan_is_read_by_ocr(contract):
    rec = record(read(contract, "good/invoice_scan.pdf"))
    assert rec["extraction"] == "ocr"
    assert float(rec["ocr_confidence"]) >= 75
    assert Decimal(rec["subtotal"]) + Decimal(rec["tax"]) == Decimal(rec["total"])


@needs_tesseract
def test_a_poor_scan_is_quarantined_not_guessed(contract):
    result = read(contract, "bad/invoice_scan_poor.pdf")
    assert [v.reason_code for v in result.file_violations] == ["OCR.LOW_CONFIDENCE"]
    assert not result.accepted
