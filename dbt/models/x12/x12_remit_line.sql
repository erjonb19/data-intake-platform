{{ config(schema='silver', materialized='view', tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Silver: every accepted 835 service payment, typed, with its adjustments summed per
-- group and reason by dip/x12feed.py (CO-45 contractual, PR-1 deductible, PR-2
-- coinsurance, the rest as other).

with deliveries as (
    select distinct on (d.file_sha256)
        d.file_sha256,
        d.source_key,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) as file_date,
        d.received_at
    from {{ source('ops', 'intake_delivery') }} as d
    where d.feed = 'healthcare.remittance_835'
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
    b.record ->> 'payment_trace' as payment_trace,
    (b.record ->> 'payment_date')::date as payment_date,
    b.record ->> 'payer_name' as payer_name,
    b.record ->> 'claim_id' as claim_id,
    b.record ->> 'claim_status' as claim_status,
    b.record ->> 'payer_claim_control' as payer_claim_control,
    b.record ->> 'claim_line_id' as claim_line_id,
    b.record ->> 'procedure_code' as procedure_code,
    (b.record ->> 'charge_amount')::numeric(12, 2) as charge_amount,
    (b.record ->> 'paid_amount')::numeric(12, 2) as paid_amount,
    (b.record ->> 'contractual_adjustment')::numeric(12, 2) as contractual_adjustment,
    (b.record ->> 'deductible_amount')::numeric(12, 2) as deductible_amount,
    (b.record ->> 'coinsurance_amount')::numeric(12, 2) as coinsurance_amount,
    (b.record ->> 'other_adjustment')::numeric(12, 2) as other_adjustment,
    (b.record ->> 'service_date')::date as service_date
from {{ source('bronze', 'csv_record') }} as b
inner join {{ source('ops', 'intake_file') }} as f
    on f.feed = b.feed and f.file_sha256 = b.file_sha256
inner join deliveries as d on d.file_sha256 = b.file_sha256
where b.feed = 'healthcare.remittance_835'
