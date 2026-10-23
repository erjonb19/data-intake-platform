{{ config(schema='gold', materialized='table', tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Gold: the current version of every 837P claim, one row per line. A claim is taken whole
-- from the newest file that has it (by business date, then received time), so a
-- replacement (frequency 7) that drops a line drops it here too, and a void (frequency 8)
-- removes the claim.
with latest as (
    select distinct on (claim_id) claim_id, file_sha256
    from {{ ref('x12_claim_line') }}
    order by claim_id, file_date desc, received_at desc, file_sha256 desc
)

select
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
inner join latest as l on l.claim_id = r.claim_id and l.file_sha256 = r.file_sha256
where r.claim_frequency <> '8'
