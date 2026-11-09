"""PDF invoices (Stage 3, S3.1): dip/pdffeed.py reads native PDFs by their text layer and
scans by OCR, in both vendor layouts, and gives the CSV engine's codes for a misread value.
The fixtures are generated and invented (tests/fixtures/finance/make_fixtures.py). The OCR
tests need Tesseract, which only the OCR task image and CI have: they skip without it."""

from __future__ import annotations

import shutil
import zlib
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


def test_a_forged_page_count_does_not_get_past_the_page_limit(contract):
    # A 25-page PDF whose page tree claims 1 page: the pages actually read are counted.
    data = _pdf(pages=25)
    assert b"/Count 25" in data
    forged = data.replace(b"/Count 25", b"/Count 1 ")  # same length, so offsets hold
    result = read(contract, "", forged)
    assert [v.reason_code for v in result.file_violations] == ["PDF.TOO_MANY_PAGES"]


# 12.8 (#97): a decompression bomb is refused while pdfminer decodes it, before it is in
# memory. The budget is lowered for speed; each bomb decodes to 4 times it. The bombs are
# real one-page PDFs whose content stream is the bomb, so pdfminer decodes them as it would
# any invoice, through whatever filters the stream names.
BOMB_LIMIT = 8 * 1024 * 1024
BOMB_SIZE = 4 * BOMB_LIMIT


def _zeros_deflated(n: int, wbits: int = 15) -> bytes:
    """`n` zero bytes, compressed in pieces (never `n` bytes in memory at once)."""
    comp, out, chunk = zlib.compressobj(9, zlib.DEFLATED, wbits), [], bytes(1 << 20)
    for _ in range(n >> 20):
        out.append(comp.compress(chunk))
    out.append(comp.flush())
    return b"".join(out)


def _lzw_zeros(n: int) -> bytes:
    """`n` zero bytes as PDF LZWDecode data, the codes written out directly: after a clear,
    code 0 is one zero and each code k from 258 up is (k - 256) zeros, the entry pdfminer's
    decoder has just added. Widths grow from 9 to 12 bits as the decoder's table reaches
    511, 1023 and 2047 entries (EarlyChange 1), and the table is cleared before 4096."""
    out, acc, bits, width = bytearray(), 0, 0, 9

    def put(code: int) -> None:
        nonlocal acc, bits
        acc, bits = (acc << width) | code, bits + width
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)

    put(256)
    left = n
    while left:
        put(0)
        left -= 1
        code = 258
        while left and code <= 4093:
            if code - 256 > left:  # the tail: a code already in the table
                put(0 if left == 1 else 256 + left)
                left = 0
                break
            put(code)
            left -= code - 256
            if code + 1 in (511, 1023, 2047):
                width += 1
            code += 1
        if left:
            put(256)
            width = 9
    put(257)
    if bits:
        out.append((acc << (8 - bits)) & 0xFF)
    return bytes(out)


def _one_page_pdf(content: bytes, filters: str, keyword: bytes = b"stream\n") -> bytes:
    """A valid one-page PDF whose page content stream is `content`, with `filters`."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << >> >>",
        b"<< /Length " + str(len(content)).encode() + b" /Filter " + filters.encode() + b" >>\n"
        + keyword + content + b"\nendstream",
    ]  # fmt: skip
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _codes(contract, monkeypatch, data: bytes) -> list[str]:
    import dip.pdffeed as pdffeed

    monkeypatch.setattr(pdffeed, "MAX_INFLATED_BYTES", BOMB_LIMIT)
    return [v.reason_code for v in read(contract, "", data).file_violations]


def _bomb(kind: str) -> bytes:
    import base64

    flate = _zeros_deflated(BOMB_SIZE)
    return {
        "flate": lambda: _one_page_pdf(flate, "/FlateDecode"),
        "flate_abbreviated": lambda: _one_page_pdf(flate, "/Fl"),
        "small_zlib_window": lambda: _one_page_pdf(_zeros_deflated(BOMB_SIZE, 10), "/FlateDecode"),
        "keyword_with_a_comment": lambda: _one_page_pdf(
            flate, "/FlateDecode", keyword=b"stream % x \n"
        ),
        "ascii85_open_no_close": lambda: _one_page_pdf(
            b"<~" + base64.a85encode(flate, wrapcol=72), "[/ASCII85Decode /FlateDecode]"
        ),
        "asciihex_no_close": lambda: _one_page_pdf(
            flate.hex().encode(), "[/ASCIIHexDecode /FlateDecode]"
        ),
        "flate_in_flate": lambda: _one_page_pdf(
            zlib.compress(flate), "[/FlateDecode /FlateDecode]"
        ),
        "three_flate_layers": lambda: _one_page_pdf(
            zlib.compress(zlib.compress(flate)), "[/FlateDecode /FlateDecode /FlateDecode]"
        ),
        "zlib_header_inside": lambda: _one_page_pdf(
            zlib.compress(b"x\x9c" + bytes(BOMB_SIZE)), "/FlateDecode"
        ),
        "lzw": lambda: _one_page_pdf(_lzw_zeros(BOMB_SIZE), "/LZWDecode"),
        "ccittfax": lambda: _one_page_pdf(
            b"\xff" * 10, "/CCITTFaxDecode /DecodeParms << /K -1 /Columns 80000 >>"
        ),
        "ccittfax_under_flate": lambda: _one_page_pdf(
            zlib.compress(b"\xff" * 10),
            "[/FlateDecode /CCITTFaxDecode] /DecodeParms [null << /K -1 /Columns 80000 >>]",
        ),
        "runlength": lambda: _one_page_pdf(
            b"\x81\x00" * (BOMB_SIZE // 128) + b"\x80", "/RunLengthDecode"
        ),
    }[kind]()


@pytest.mark.parametrize(
    "kind",
    ["flate", "flate_abbreviated", "small_zlib_window", "keyword_with_a_comment",
     "ascii85_open_no_close", "asciihex_no_close", "flate_in_flate", "three_flate_layers",
     "zlib_header_inside", "lzw", "runlength", "ccittfax", "ccittfax_under_flate"],
)  # fmt: skip
def test_a_decompression_bomb_is_refused(contract, monkeypatch, kind):
    data = _bomb(kind)
    assert len(data) < BOMB_LIMIT  # small on disk
    assert _codes(contract, monkeypatch, data) == ["PDF.DECOMPRESSION_BOMB"]


def test_the_lzw_bomb_is_what_pdfminer_decodes():
    # The test's own LZW encoder, checked against pdfminer's decoder on a small input.
    from pdfminer.lzw import lzwdecode

    assert lzwdecode(_lzw_zeros(100_000)) == bytes(100_000)


def test_a_small_compressed_page_is_read_and_the_decoders_are_restored(contract, monkeypatch):
    from pdfminer import pdftypes

    before = (pdftypes.zlib, pdftypes.lzwdecode, pdftypes.rldecode, pdftypes.decompress_corrupted)
    content = b"BT /F1 12 Tf 72 720 Td (x) Tj ET"
    for kind, body in [("/FlateDecode", zlib.compress(content)), ("/RunLengthDecode", b"\x80")]:
        assert _codes(contract, monkeypatch, _one_page_pdf(body, kind)) != [
            "PDF.DECOMPRESSION_BOMB"
        ]
    after = (pdftypes.zlib, pdftypes.lzwdecode, pdftypes.rldecode, pdftypes.decompress_corrupted)
    assert after == before


def test_a_flate_compressed_invoice_is_still_read(contract):
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pageCompression=1, invariant=1)
    y = 720
    for line in ["Acme Supplies", "Vendor ID V1001", "Invoice number: INV-000001",
                 "Invoice date: 2026-10-14", "Due date: 2026-11-13", "PO number: PO-000001",
                 "Payment terms: NET30", "Currency: USD", "Subtotal: 1,000.00",
                 "Tax: 88.75", "Total: 1,088.75"]:  # fmt: skip
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    assert b"/FlateDecode" in buf.getvalue()
    assert record(read(contract, "", buf.getvalue()))["total"] == "1088.75"


def test_a_png_predictor_row_wider_than_its_data_is_refused(contract, monkeypatch):
    # pdfminer allocates the row from /Columns before reading: a GB from a few bytes.
    data = _one_page_pdf(
        zlib.compress(bytes(4)), "/FlateDecode /DecodeParms << /Predictor 12 /Columns 100000000 >>"
    )
    assert _codes(contract, monkeypatch, data) == ["PDF.DECOMPRESSION_BOMB"]


def test_a_normal_png_predictor_still_decodes():
    from dip.pdffeed import _png_predictor

    rows = b"\x00abc\x00def"  # two rows of 3 bytes, filter type 0 (none)
    calls = []
    apply = _png_predictor(lambda *a: calls.append(a) or b"ok")
    assert apply(12, 1, 3, 8, rows) == b"ok" and calls


def test_a_one_bit_predictor_row_cannot_pass_by_its_byte_count(contract, monkeypatch):
    # 1 bit per component: a 16 MB row over 16 MB of data, but 134 M columns of row buffer.
    data = _one_page_pdf(
        _zeros_deflated(16 << 20),
        "/FlateDecode /DecodeParms << /Predictor 12 /BitsPerComponent 1 /Columns 134217728 >>",
    )
    import dip.pdffeed as pdffeed

    monkeypatch.setattr(pdffeed, "MAX_INFLATED_BYTES", 32 << 20)  # the data fits the budget
    assert [v.reason_code for v in read(contract, "", data).file_violations] == [
        "PDF.DECOMPRESSION_BOMB"
    ]
