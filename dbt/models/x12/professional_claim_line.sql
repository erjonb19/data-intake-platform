{{ config(schema='gold', materialized='table', tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Gold: one row per 837P service line ever submitted, from the newest file that has it (by
-- business date, then received time): a corrected resubmission replaces the earlier line.
select distinct on (r.claim_line_id)
    r.claim_line_id,
    r.claim_id,
    r.line_number,
    r.member_id,
    r.payer_id,
    r.billing_npi,
    r.place_of_service,
    r.claim_frequency,
    r.claim_total,
    r.procedure_qualifier,
    r.procedure_code,
    r.charge_amount,
    r.units,
    r.service_date,
    r.file_date,
    r.received_at,
    r.file_sha256 as source_file_sha256
from {{ ref('x12_claim_line') }} as r
order by r.claim_line_id, r.file_date desc, r.received_at desc, r.file_sha256 desc
