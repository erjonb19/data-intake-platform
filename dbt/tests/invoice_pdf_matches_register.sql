-- Finance (S3.1): every invoice PDF matches the AP register (gold.invoice_match). A warning:
-- a mismatch is a finding for AP to work (a PDF the register lacks, a different total),
-- not a reason to drop the document.
{{ config(severity='warn', tags=['finance.vendor_invoice_pdf'],
          meta={'dq_category': 'conformance', 'dqd_check': 'crossSourceMatch'}) }}

select invoice_number, match_status, pdf_total, register_total, extraction
from {{ ref('invoice_match') }}
where match_status <> 'matched'
