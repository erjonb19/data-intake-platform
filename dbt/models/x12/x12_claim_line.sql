{{ config(schema='silver', materialized='view', tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Silver: every accepted 837P service line, typed. dip/x12feed.py already checked the
-- envelope and each value against the contract, so the casts cannot fail. file_date is the
-- business date in the name (claims837_YYYYMMDD.x12), else the UTC date received.

with deliveries as (
    select distinct on (d.file_sha256)
        d.file_sha256,
        d.source_key,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) as file_date,
        d.received_at
    from {{ source('ops', 'intake_delivery') }} as d
    where d.feed = 'healthcare.claims_837'
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
    b.record ->> 'claim_id' as claim_id,
    b.record ->> 'claim_line_id' as claim_line_id,
    (b.record ->> 'line_number')::integer as line_number,
    b.record ->> 'member_id' as member_id,
    b.record ->> 'payer_id' as payer_id,
    b.record ->> 'billing_npi' as billing_npi,
    b.record ->> 'place_of_service' as place_of_service,
    b.record ->> 'claim_frequency' as claim_frequency,
    (b.record ->> 'claim_total')::numeric(12, 2) as claim_total,
    b.record ->> 'procedure_qualifier' as procedure_qualifier,
    b.record ->> 'procedure_code' as procedure_code,
    (b.record ->> 'charge_amount')::numeric(12, 2) as charge_amount,
    (b.record ->> 'units')::numeric(8, 2) as units,
    (b.record ->> 'service_date')::date as service_date
from {{ source('bronze', 'csv_record') }} as b
inner join {{ source('ops', 'intake_file') }} as f
    on f.feed = b.feed and f.file_sha256 = b.file_sha256
inner join deliveries as d on d.file_sha256 = b.file_sha256
where b.feed = 'healthcare.claims_837'
