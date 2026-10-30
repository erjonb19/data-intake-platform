# Runbook 7: an unreadable scan

**Signal.** A PDF invoice quarantined whole with `OCR.LOW_CONFIDENCE` (mean Tesseract word
confidence under the contract's `min_ocr_confidence`, 75), or `PDF.MISSING_FIELD:<col>`
when OCR read the page but a label was not found. Related: `OCR.TIMEOUT`, `OCR.FAILED`,
`PDF.TOO_MANY_PAGES`, `PDF.PAGE_TOO_LARGE` (limits that stop one file from holding the OCR
pod), and `OCR.UNAVAILABLE` (the task ran without the OCR image: a deployment problem, not
the vendor's).

**A PDF cut short** in transfer is `PDF.INVALID` (the parser cannot read it at all; the
cross-reference table is gone): `python -m dip break truncated_pdf <good.pdf> <out.pdf>`
keeps the first half of the bytes. Nothing on the page can be read, so the only fix is the
vendor's: `request_resend`.

**See it:** the committed fixture `tests/fixtures/finance/bad/invoice_scan_poor.pdf`
(70 dpi, blurred and speckled) scores 6.7, and the E2E expects `OCR.LOW_CONFIDENCE`.

## Evidence

1. `list_quarantine_groups(feed="finance.vendor_invoice_pdf")`: the code and the detail
   ("mean OCR word confidence 6.7 is under 75.0").
2. `get_dag_run("intake__finance__vendor_invoice_pdf")` and the intake task's log: which
   image the task ran in (the OCR image, `DIP_OCR_IMAGE`).
3. `gold.invoice_match`: is the invoice in the register already (the Excel feed)?

## Decide

| What you see | Action |
|---|---|
| A poor scan | `request_resend` asking for the native PDF or a 200+ dpi scan. Never lower `min_ocr_confidence` to let it through: a guessed total is worse than a quarantined one |
| `OCR.UNAVAILABLE` | Fix the deployment (`up.sh` builds and loads the OCR image); then `replay` |

## Check

The resent PDF `loaded`; `gold.invoice_match` shows it `matched` (or the mismatch status
AP must work: `total_differs`, `not_in_register`).
