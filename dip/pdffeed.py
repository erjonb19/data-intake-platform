"""Validate a PDF document (one record per file) against a `pdf` data contract (Stage 3).

A vendor invoice arrives as a PDF: either native (it has a text layer) or scanned (only an
image of the page). The reader:

1. takes the text layer with pdfplumber; a page without one is OCR'd with Tesseract
   (pytesseract), keeping each word's confidence. The page is rendered at the scan's own
   resolution, capped at `format.ocr_dpi` (default 300): upsampling a 200 dpi scan to 300
   only blurs it (on the test scan, mean confidence 91.0 at 200 dpi, 74.9 at 300).
   Tesseract runs only in the OCR task image (images/ocr) and on CI, never in the main
   Airflow image;
2. if the mean word confidence of an OCR'd file is under `format.min_ocr_confidence`
   (default 75), quarantines the whole file with `OCR.LOW_CONFIDENCE`: a guessed number is
   worse than a quarantined one;
3. finds each field by its label (`format.fields`: column -> label), whatever the layout:
   "Invoice number: INV-1" and "Invoice number   INV-1" both work. `first_line: true` takes
   the first line of the page (a letterhead). A field it cannot find is
   `PDF.MISSING_FIELD:<column>`;
4. normalizes values the way vendors print them (amounts lose thousands separators and
   currency signs) and runs the result through the CSV engine's row checks, so a misread
   value gets the same codes as in any other format (`VALUE.PATTERN:invoice_number`).

Two columns describe the reading itself: `extraction` (native or ocr) and
`ocr_confidence` (empty for native files), so gold can tell a typed total from a read one.

A page with neither text nor an image (a blank trailing page) is skipped, so it never
turns a typed invoice into a "scan".

File-level codes: PDF.INVALID (anything the parser cannot read, wherever it fails),
PDF.EMPTY, PDF.TOO_MANY_PAGES (over 20), FILE.TOO_LARGE (over 20 MB), PDF.PAGE_TOO_LARGE
(a page that would render over 40 Mpx), OCR.TIMEOUT (60 s per page), OCR.FAILED (Tesseract
could not read a page), OCR.UNAVAILABLE (the
task is not in the OCR image), OCR.LOW_CONFIDENCE, PDF.MISSING_FIELD:<column>,
PDF.DECOMPRESSION_BOMB (its Flate streams would inflate past MAX_INFLATED_BYTES). The limits
bound what one file can cost.

The decompression check (12.8, #97) runs before pdfminer parses anything: it finds every
`stream` keyword in the raw bytes and inflates each body that is zlib data in bounded
chunks, throwing the output away, so the check itself never holds more than a chunk
(`_inflated_size`). An ASCII85 or ASCIIHex body is decoded first, and a Flate layer inside
a Flate layer is inflated too. A file whose streams add up to more than MAX_INFLATED_BYTES
is refused. A legitimate invoice inflates to a few MB, and 20 grayscale 300 dpi scans fit;
the budget is a third of the OCR pod's memory, since pdfminer keeps what it inflates.
Streams in another form (LZW, run-length, an encrypted stream) are not counted and are left
to the pod's memory limit, as every stream was before.
"""

from __future__ import annotations

import base64
import io
import re
import zlib
from datetime import UTC, datetime
from typing import Any

from dip.csvfeed import CsvResult, check_rows
from dip.fhir import Violation

DEFAULT_DPI = 300
DEFAULT_MIN_CONFIDENCE = 75.0
# Untrusted input: one invoice is a page or two, so anything far bigger is refused before it
# can exhaust the task pod (768Mi) or wedge the landing folder.
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_PIXELS = 40_000_000  # per rendered page: a letter page at 600 dpi is 33.7 Mpx
OCR_TIMEOUT_S = 60  # per page
# Every Flate stream inflated, in total. A third of the OCR task pod's 768Mi: pdfminer keeps
# what it inflates, so a file past this would not have fit anyway. A grayscale letter page
# at 300 dpi is 8.4 MB raw, so 20 of them (MAX_PAGES) fit; scans are mostly JPEG (DCT),
# which is not inflated at all.
MAX_INFLATED_BYTES = 256 * 1024 * 1024
_INFLATE_CHUNK = 1024 * 1024
_STREAM = re.compile(rb"(?<!end)stream(?:\r\n|\n|\r)")


class _Refused(Exception):
    """A file-level violation found while reading; the reader stops at the first."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.violation = Violation(code, detail)


def _scan_dpi(page: Any, cap: int) -> int:
    """The resolution of the page's largest embedded image by displayed area (a scan is one
    page-sized image; a logo is not), at most `cap`; `cap` for a page with no image."""
    images = [
        im
        for im in page.images
        if im.get("srcsize") and im["x1"] > im["x0"] and im["bottom"] > im["top"]
    ]
    if not images:
        return cap
    im = max(images, key=lambda i: (i["x1"] - i["x0"]) * (i["bottom"] - i["top"]))
    return max(72, min(cap, round(im["srcsize"][0] / ((im["x1"] - im["x0"]) / 72))))


def _ocr(page: Any, cap: int) -> tuple[str, list[float]]:
    try:
        import pytesseract
    except ImportError as exc:
        raise _Refused("OCR.UNAVAILABLE", f"a scanned page and no OCR in this task: {exc}") from exc
    dpi = _scan_dpi(page, cap)
    pixels = (page.width * dpi / 72) * (page.height * dpi / 72)
    if pixels > MAX_PIXELS:
        raise _Refused("PDF.PAGE_TOO_LARGE",
                       f"page {page.page_number} would render at {pixels / 1e6:.0f} Mpx"
                       f" (limit {MAX_PIXELS / 1e6:.0f})")  # fmt: skip
    image = page.to_image(resolution=dpi).original.convert("L")
    try:
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT_S
        )
    except RuntimeError as exc:  # pytesseract's timeout, or a TesseractError (a subclass)
        code = "OCR.TIMEOUT" if "timeout" in str(exc).lower() else "OCR.FAILED"
        raise _Refused(code, f"page {page.page_number}: {str(exc)[:300]}") from exc
    except OSError as exc:  # the tesseract binary is missing
        raise _Refused("OCR.UNAVAILABLE", f"a scanned page and no OCR in this task: {exc}") from exc
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences = []
    for i, word in enumerate(data["text"]):
        conf = float(data["conf"][i])
        if word.strip() and conf >= 0:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(word)
            confidences.append(conf)
    return "\n".join(" ".join(ws) for _, ws in sorted(lines.items())), confidences


def _inflated_size(data: bytes, limit: int) -> int:
    """How many bytes the file's Flate streams inflate to, counted without keeping them, and
    stopping as soon as the total passes `limit`.

    The body after each `stream` keyword is looked at, not its dictionary's /Filter, which
    can be hidden: if it is ASCII85 or ASCIIHex text (`[/ASCII85Decode /FlateDecode]`, as
    reportlab writes), that is decoded first, bounded by the file's own size. Zlib data is
    then inflated from its first byte until zlib says it ended, not up to the next
    `endstream`, which compressed bytes can contain; a second Flate layer inside is inflated
    as it streams. Every layer's output counts. Input goes in 64 KiB pieces and output comes
    out at most `_INFLATE_CHUNK` at a time, so the check holds little memory whatever the
    ratio. A stream in any other form (an image, LZW, an encrypted stream) is skipped; one
    that is cut short counts what it gave."""
    total, pos = 0, 0
    while total <= limit and (m := _STREAM.search(data, pos)) is not None:
        start = pos = m.end()
        body = _text_decoded(data, start)
        if body is None:
            body = memoryview(data)[start:]
        if not _zlib_header(bytes(body[:2])):
            continue
        total = _count_inflated(body, total, limit)
    return total


def _count_inflated(body: Any, total: int, limit: int) -> int:
    """`total` plus what `body` inflates to, through at most one inner Flate layer."""
    outer, inner = zlib.decompressobj(), None
    try:
        for start in range(0, len(body), 65536):
            out = outer.decompress(body[start : start + 65536], _INFLATE_CHUNK)
            while True:
                total += len(out)
                if inner is None and len(out) >= 2 and _zlib_header(out[:2]):
                    inner = zlib.decompressobj()
                if inner is not None and not inner.eof:
                    total += len(inner.decompress(out, _INFLATE_CHUNK))
                    while inner.unconsumed_tail and total <= limit:
                        total += len(inner.decompress(inner.unconsumed_tail, _INFLATE_CHUNK))
                if total > limit or not outer.unconsumed_tail:
                    break
                out = outer.decompress(outer.unconsumed_tail, _INFLATE_CHUNK)
            if total > limit or outer.eof:
                break
    except zlib.error:
        pass
    return total


_A85 = re.compile(rb"[!-uz\s]*~>")
_AHX = re.compile(rb"[0-9A-Fa-f\s]*>")


def _text_decoded(data: bytes, start: int) -> bytes | None:
    """The stream body at `start` decoded from ASCII85 or ASCIIHex, or None if it is
    neither. Both only shrink data, so this is bounded by the file's size."""
    head = data[start : start + 2]
    if _zlib_header(head):
        return None
    if m := _A85.match(data, start):
        try:
            # Without the closing "~>"; ">" alone is an ASCII85 digit.
            return base64.a85decode(
                data[start : m.end() - 2], adobe=False, ignorechars=b" \t\n\r\x0b\x0c"
            )
        except ValueError:
            return None
    if m := _AHX.match(data, start):
        digits = re.sub(rb"\s", b"", data[start : m.end() - 1])
        try:
            return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())
        except ValueError:
            return None
    return None


def _zlib_header(two: bytes) -> bool:
    """A zlib header: deflate (CM 8) with any window size, and the FCHECK that makes the two
    bytes a multiple of 31. Any of these, not only the common 0x78, is inflated by pdfminer."""
    if len(two) != 2:
        return False
    return two[0] & 0x0F == 8 and two[0] >> 4 <= 7 and (two[0] << 8 | two[1]) % 31 == 0


def _page_count(pdf: Any) -> int:
    """From the page tree's /Count, before pdfplumber builds a Page for every page."""
    from pdfminer.pdftypes import resolve1

    return int(resolve1(resolve1(pdf.doc.catalog["Pages"])["Count"]))


def _read(data: bytes, fmt: dict[str, Any]) -> tuple[str, float | None]:
    try:
        import pdfplumber
    except ImportError as exc:  # the task is not running in the OCR image
        raise _Refused("OCR.UNAVAILABLE", f"no PDF reader in this task: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise _Refused("FILE.TOO_LARGE", f"{len(data)} bytes (limit {MAX_BYTES})")
    inflated = _inflated_size(data, MAX_INFLATED_BYTES)
    if inflated > MAX_INFLATED_BYTES:
        detail = f"its compressed streams inflate past {MAX_INFLATED_BYTES} bytes"
        raise _Refused("PDF.DECOMPRESSION_BOMB", detail)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        count = _page_count(pdf)
        if count == 0:
            raise _Refused("PDF.EMPTY", "the PDF has no pages")
        if count > MAX_PAGES:
            raise _Refused("PDF.TOO_MANY_PAGES", f"{count} pages (limit {MAX_PAGES})")
        texts, confidences, ocr_used = [], [], False
        # /Count above is a cheap early exit; the page tree itself (the Kids) can hold more
        # pages than a forged /Count says, so the pages actually read are counted too.
        for n, page in enumerate(pdf.pages, start=1):
            if n > MAX_PAGES:
                detail = f"more than {MAX_PAGES} pages (the page tree says {count})"
                raise _Refused("PDF.TOO_MANY_PAGES", detail)
            text = page.extract_text() or ""
            if text.strip():
                texts.append(text)
            elif page.images:  # a scanned page
                ocr_used = True
                text, conf = _ocr(page, int(fmt.get("ocr_dpi", DEFAULT_DPI)))
                texts.append(text)
                confidences.extend(conf)
            # else: a blank page (a trailing empty page of a typed invoice): nothing to read
    if not ocr_used:
        return "\n".join(texts), None
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(texts), round(mean, 1)


def read_text(
    data: bytes, fmt: dict[str, Any], result: CsvResult
) -> tuple[str, float | None] | None:
    """The document's text, and the mean OCR confidence (None for a native file), or None
    with a file violation in `result`. Never raises on a bad file: a PDF that breaks the
    parser anywhere (pdfminer parses lazily, so errors can surface page by page) is
    PDF.INVALID, and the intake moves it to quarantine like any other bad file."""
    try:
        return _read(data, fmt)
    except _Refused as refused:
        result.file_violations.append(refused.violation)
    except Exception as exc:  # noqa: BLE001 - untrusted input: any parser failure
        result.file_violations.append(
            Violation("PDF.INVALID", f"not a readable PDF: {type(exc).__name__}: {exc}"[:500])
        )
    return None


def _value(text: str, label: str) -> str | None:
    """The rest of the line after `label`: "Label: value", or "Label   value" as a table
    or an OCR'd line gives it. Never a longer label that starts the same way: "Tax ID: 9"
    is not the value of "Tax" (the rest of a label-only match may hold no colon)."""
    lab = re.escape(label)
    for pattern in (rf"(?im)^[ \t]*{lab}[ \t]*:[ \t]*(\S[^\n]*?)[ \t]*$",
                    rf"(?im)^[ \t]*{lab}[ \t]+(?![^\n]*:)(\S[^\n]*?)[ \t]*$"):  # fmt: skip
        if m := re.search(pattern, text):
            return m.group(1)
    return None


def _normalize(value: str, col_type: str) -> str:
    if col_type in ("decimal", "integer"):
        return re.sub(r"[,\s$€£]", "", value)
    return value.strip()


def validate_pdf(data: bytes, contract: Any, received_at: datetime | None = None) -> CsvResult:
    spec = contract.spec
    fmt = spec["format"]
    today = (received_at or datetime.now(UTC)).astimezone(UTC).date()
    result = CsvResult()
    read = read_text(data, fmt, result)
    if read is None:
        return result
    text, confidence = read
    minimum = float(fmt.get("min_ocr_confidence", DEFAULT_MIN_CONFIDENCE))
    if confidence is not None and confidence < minimum:
        result.file_violations.append(Violation(
            "OCR.LOW_CONFIDENCE", f"mean OCR word confidence {confidence} is under {minimum}"
        ))  # fmt: skip
        return result
    types = {c["name"]: c.get("type", "string") for c in spec["columns"]}
    record: dict[str, str] = {}
    for column, rule in fmt["fields"].items():
        if isinstance(rule, dict) and rule.get("first_line"):
            value = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
        else:
            value = _value(text, rule)
        if value is None:
            result.file_violations.append(
                Violation(f"PDF.MISSING_FIELD:{column}", f"no {rule!r} found in the document")
            )
            continue
        record[column] = _normalize(value, types.get(column, "string"))
    if result.file_violations:
        return result
    record["extraction"] = "native" if confidence is None else "ocr"
    record["ocr_confidence"] = "" if confidence is None else str(confidence)
    header = [c["name"] for c in spec["columns"]]
    return check_rows([header, [record.get(h, "") for h in header]], spec, today, result)
