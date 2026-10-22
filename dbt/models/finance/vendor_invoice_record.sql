{{ config(schema='silver', materialized='view', tags=['file_feed', 'finance.vendor_invoice', 'finance.vendor_invoice_pdf']) }}

-- Silver: every accepted invoice row, typed (finance pack, S2.5). The intake read the
-- workbook (dip/excelfeed.py) and checked each value against the contract, so the casts
-- cannot fail. Amounts stay unscaled numeric: Excel stores them as numbers, and the
-- text the reader made of them (12.5, not 12.50) casts back exactly.
with deliveries as (
    select distinct on (d.file_sha256)
        d.file_sha256,
        d.source_key,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) as file_date,
        d.received_at
    from {{ source('ops', 'intake_delivery') }} as d
    where d.feed = 'finance.vendor_invoice'
    order by
        d.file_sha256,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) desc,
        d.received_at desc,
        d.source_key desc
)

select
    b.file_sha256,
    b.row_number,
    d.source_key,
    d.file_date,
    d.received_at,
    f.contract_version,
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
    (b.record ->> 'total')::numeric as total
from {{ source('bronze', 'csv_record') }} as b
inner join {{ source('ops', 'intake_file') }} as f
    on f.feed = b.feed and f.file_sha256 = b.file_sha256
inner join deliveries as d on d.file_sha256 = b.file_sha256
where b.feed = 'finance.vendor_invoice'
