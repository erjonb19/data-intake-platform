-- 837P (S4.3): a claim's total (CLM02) is the sum of its lines' charges (SV102), as the
-- implementation guide requires. A warning: with row quarantine, a claim whose line was
-- quarantined loads without it, and its total then differs for a good reason.
{{ config(severity='warn', tags=['healthcare.claims_837', 'healthcare.remittance_835'],
          meta={'dq_category': 'conformance', 'dqd_check': 'plausibleValueHigh'}) }}

select claim_id, max(claim_total) as claim_total, sum(charge_amount) as line_charges
from {{ ref('professional_claim_line') }}
group by claim_id
having max(claim_total) <> sum(charge_amount)
