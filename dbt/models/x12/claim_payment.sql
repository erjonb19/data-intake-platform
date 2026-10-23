{{ config(schema='gold', materialized='table',
          tags=['file_feed', 'healthcare.claims_837', 'healthcare.remittance_835']) }}

-- Gold: every submitted claim line with what the payer did with it, billed and paid side
-- by side. Built by both feeds' DAGs (it reads both).
--   not_remitted   no 835 line yet
--   denied         the claim was denied (CLP02 = 4), or nothing was paid on a charge
--   paid_in_full   paid = charge
--   paid_adjusted  paid less than the charge, the rest explained by the adjustments
select
    c.claim_line_id,
    c.claim_id,
    c.line_number,
    c.member_id,
    c.payer_id,
    c.procedure_code,
    c.service_date,
    c.charge_amount,
    r.paid_amount,
    r.contractual_adjustment,
    r.deductible_amount + r.coinsurance_amount as patient_responsibility,
    r.other_adjustment,
    r.payment_date,
    r.payment_trace,
    case
        when r.claim_line_id is null then 'not_remitted'
        when r.claim_status = '4' or (r.paid_amount = 0 and c.charge_amount > 0) then 'denied'
        when r.paid_amount = c.charge_amount then 'paid_in_full'
        else 'paid_adjusted'
    end as payment_status,
    r.payment_date - c.service_date as days_to_payment,
    c.source_file_sha256
from {{ ref('professional_claim_line') }} as c
left join {{ ref('remittance_line') }} as r on r.claim_line_id = c.claim_line_id
