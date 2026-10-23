-- 835 against 837P (S4.3): the charge the payer remits on a line is the charge that was
-- billed. A warning: a payer may split or bundle lines, which a person should look at.
{{ config(severity='warn', tags=['healthcare.remittance_835', 'healthcare.claims_837'],
          meta={'dq_category': 'plausibility', 'dqd_check': 'plausibleValueHigh'}) }}

select r.claim_line_id, c.charge_amount as billed, r.charge_amount as remitted
from {{ ref('remittance_line') }} as r
inner join {{ ref('professional_claim_line') }} as c on c.claim_line_id = r.claim_line_id
where r.charge_amount <> c.charge_amount
