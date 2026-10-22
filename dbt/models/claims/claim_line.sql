{{ config(schema='gold', materialized='table', tags=['file_feed', 'healthcare.claims']) }}

-- Gold: one row per claim line ever received, from the newest file that has it (by
-- business date, then received time), so a corrected resend replaces the earlier version
-- and a late copy of an older file never does. Claims are matched to eligibility in the
-- DQ tests (coverage, member known), not here: a claim outside coverage is a data quality
-- finding for the payer, not a reason to drop the line.
select distinct on (r.claim_line_id)
    r.claim_line_id,
    r.claim_id,
    r.line_number,
    r.member_id,
    r.service_date,
    r.place_of_service,
    r.procedure_code,
    r.procedure_system,
    r.billed_amount,
    r.allowed_amount,
    r.paid_amount,
    r.coinsurance_amount,
    r.deductible_amount,
    r.payer,
    r.file_date,
    r.received_at,
    r.file_sha256 as source_file_sha256
from {{ ref('claim_record') }} as r
order by r.claim_line_id, r.file_date desc, r.received_at desc, r.file_sha256 desc
