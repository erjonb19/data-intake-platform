{{ config(schema='silver', materialized='view', tags=['file_feed', 'finance.vendor_invoice', 'finance.vendor_invoice_pdf']) }}

-- Silver: every accepted invoice PDF (one row per file), typed (S3.1). dip/pdffeed.py read
-- the text layer, or OCR'd a scan, and checked the fields against the contract.
-- `extraction` says which, and `ocr_confidence` is the scan's mean word confidence.
with deliveries as (
    select distinct on (d.file_sha256)
        d.file_sha256,
        d.source_key,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) as file_date,
        d.received_at
    from {{ source('ops', 'intake_delivery') }} as d
    where d.feed = 'finance.vendor_invoice_pdf'
    order by
        d.file_sha256,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) desc,
        d.received_at desc,
        d.source_key desc
)

select
    b.file_sha256,
    d.source_key,
    d.file_date,
    d.received_at,
    b.record ->> 'invoice_number' as invoice_number,
    b.record ->> 'vendor_id' as vendor_id,
    b.record ->> 'vendor_name' as vendor_name,
    (b.record ->> 'invoice_date')::date as invoice_date,
    (b.record ->> 'due_date')::date as due_date,
    b.record ->> 'po_number' as po_number,
    b.record ->> 'payment_terms' as payment_terms,
    b.record ->> 'currency' as currency,
    (b.record ->> 'subtotal')::numeric as subtotal,
    (b.record ->> 'tax')::numeric as tax,
    (b.record ->> 'total')::numeric as total,
    b.record ->> 'extraction' as extraction,
    nullif(b.record ->> 'ocr_confidence', '')::numeric as ocr_confidence
from {{ source('bronze', 'csv_record') }} as b
inner join deliveries as d on d.file_sha256 = b.file_sha256
where b.feed = 'finance.vendor_invoice_pdf'
