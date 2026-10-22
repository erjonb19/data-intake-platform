{{ config(schema='gold', materialized='table', tags=['file_feed', 'finance.vendor_invoice', 'finance.vendor_invoice_pdf']) }}

-- Gold: one row per invoice number ever received, from the newest register that has it
-- (by business date, then received time). Stage 3 matches native and scanned PDF invoices
-- to these rows by invoice_number and total.
select distinct on (r.invoice_number)
    r.invoice_number,
    r.vendor_id,
    r.vendor_name,
    r.invoice_date,
    r.due_date,
    r.po_number,
    r.payment_terms,
    r.currency,
    r.subtotal,
    r.tax,
    r.total,
    r.file_date,
    r.received_at,
    r.file_sha256 as source_file_sha256
from {{ ref('vendor_invoice_record') }} as r
order by r.invoice_number, r.file_date desc, r.received_at desc, r.file_sha256 desc
