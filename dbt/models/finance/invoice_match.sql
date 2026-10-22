{{ config(schema='gold', materialized='table', tags=['file_feed', 'finance.vendor_invoice_pdf']) }}

-- Gold (S3.1): each invoice PDF matched to the AP register (gold.vendor_invoice, the Excel
-- feed) by invoice number, from the newest PDF of each number. The first check that fails
-- names the status, so every mismatch has one reason:
--   not_in_register  no register row with this number (yet)
--   vendor_differs   a different vendor id
--   po_differs       a different PO number
--   total_differs    a different total (to the cent)
--   matched          all of the above agree
-- The register is looked up, not ref()'d, so this feed's DAG does not have to build the
-- Excel feed's models (they are the other feed's).
-- depends_on: {{ ref('vendor_invoice_pdf_record') }}
{% set register = adapter.get_relation(database=target.database, schema='gold',
                                        identifier='vendor_invoice') %}
with pdfs as (
    select distinct on (invoice_number) *
    from {{ ref('vendor_invoice_pdf_record') }}
    order by invoice_number, file_date desc, received_at desc, file_sha256 desc
)

select
    p.invoice_number,
    p.vendor_id,
    p.po_number,
    p.total as pdf_total,
    r.total as register_total,
    p.extraction,
    p.ocr_confidence,
    case
        when r.invoice_number is null then 'not_in_register'
        when r.vendor_id is distinct from p.vendor_id then 'vendor_differs'
        when r.po_number is distinct from p.po_number then 'po_differs'
        when abs(r.total - p.total) > 0.005 then 'total_differs'
        else 'matched'
    end as match_status,
    p.file_date,
    p.received_at,
    p.file_sha256 as source_file_sha256
from pdfs as p
{% if register %}
left join {{ register }} as r on r.invoice_number = p.invoice_number
{% else %}
left join (select null::text as invoice_number, null::text as vendor_id,
                  null::text as po_number, null::numeric as total) as r on false
{% endif %}
