{{ config(schema='gold', materialized='table', tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Gold: one row per remitted service line, from the newest 835 that pays it: a later
-- remittance (a reprocessed or reversed claim) replaces the earlier one.
select distinct on (r.claim_line_id)
    r.claim_line_id,
    r.claim_id,
    r.claim_status,
    r.payer_claim_control,
    r.payment_trace,
    r.payment_date,
    r.payer_name,
    r.procedure_code,
    r.charge_amount,
    r.paid_amount,
    r.contractual_adjustment,
    r.deductible_amount,
    r.coinsurance_amount,
    r.other_adjustment,
    r.service_date,
    r.file_date,
    r.received_at,
    r.file_sha256 as source_file_sha256
from {{ ref('x12_remit_line') }} as r
order by r.claim_line_id, r.file_date desc, r.received_at desc, r.file_sha256 desc
