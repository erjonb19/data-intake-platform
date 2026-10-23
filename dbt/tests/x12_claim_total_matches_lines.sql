-- 837P (S4.3): a claim's total (CLM02) is the sum of its lines' charges (SV102), as the
-- implementation guide requires. Checked on the newest version of each claim.
{{ config(tags=['healthcare.claims_837', 'healthcare.remittance_835'],
          meta={'dq_category': 'conformance', 'dqd_check': 'plausibleValueHigh'}) }}

select claim_id, max(claim_total) as claim_total, sum(charge_amount) as line_charges
from {{ ref('professional_claim_line') }}
group by claim_id
having count(distinct claim_total) > 1 or max(claim_total) <> sum(charge_amount)
