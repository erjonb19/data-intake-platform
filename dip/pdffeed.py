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

File-level codes: PDF.INVALID (not a readable PDF), PDF.EMPTY (no pages), OCR.UNAVAILABLE
(an image-only file where Tesseract is not installed: the task image is wrong),
OCR.LOW_CONFIDENCE, PDF.MISSING_FIELD:<column>.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from typing import Any

from dip.csvfeed import CsvResult, check_rows
from dip.fhir import Violation

DEFAULT_DPI = 300
DEFAULT_MIN_CONFIDENCE = 75.0
MAX_PAGES = 20


def _scan_dpi(page: Any, cap: int) -> int:
    """The resolution of the page's largest embedded image (a scan is one image per page),
    at most `cap`; `cap` for a page with no image."""
    sizes = [
        (im["srcsize"][0] / ((im["x1"] - im["x0"]) / 72))
        for im in page.images
        if im.get("srcsize") and im["x1"] > im["x0"]
    ]
    return max(72, min(cap, round(max(sizes)))) if sizes else cap


def _ocr(page: Any, cap: int) -> tuple[str, list[float]]:
    import pytesseract

    image = page.to_image(resolution=_scan_dpi(page, cap)).original.convert("L")
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences = []
    for i, word in enumerate(data["text"]):
        conf = float(data["conf"][i])
        if word.strip() and conf >= 0:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(word)
            confidences.append(conf)
    return "\n".join(" ".join(ws) for _, ws in sorted(lines.items())), confidences


def read_text(
    data: bytes, fmt: dict[str, Any], result: CsvResult
) -> tuple[str, float | None] | None:
    """The document's text, and the mean OCR confidence (None for a native file)."""
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        result.file_violations.append(Violation("PDF.INVALID", f"not a readable PDF: {exc}"))
        return None
    with pdf:
        if not pdf.pages:
            result.file_violations.append(Violation("PDF.EMPTY", "the PDF has no pages"))
            return None
        pages = pdf.pages[:MAX_PAGES]
        texts, confidences, ocr_used = [], [], False
        for page in pages:
            text = page.extract_text() or ""
            if text.strip():
                texts.append(text)
                continue
            ocr_used = True
            try:
                text, conf = _ocr(page, int(fmt.get("ocr_dpi", DEFAULT_DPI)))
            except (ImportError, OSError, RuntimeError) as exc:
                result.file_violations.append(
                    Violation("OCR.UNAVAILABLE", f"image-only page and no OCR here: {exc}")
                )
                return None
            texts.append(text)
            confidences.extend(conf)
    if not ocr_used:
        return "\n".join(texts), None
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(texts), round(mean, 1)


def _value(text: str, label: str) -> str | None:
    """The rest of the line after `label`, with any ':' and spacing in between."""
    m = re.search(rf"(?im)^[ \t]*{re.escape(label)}\s*:?[ \t]+(\S.*?)[ \t]*$", text)
    return m.group(1) if m else None


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
