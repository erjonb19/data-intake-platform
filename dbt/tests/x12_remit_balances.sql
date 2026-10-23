-- 835 (S4.3): every remitted line balances: charge - adjustments = paid. A line that does
-- not is an 835 the payer built wrongly (or one that lost a CAS segment), so the money it
-- reports cannot be used. An error: the remittance mart would be wrong.
{{ config(tags=['healthcare.claims_837', 'healthcare.remittance_835'],
          meta={'dq_category': 'conformance', 'dqd_check': 'plausibleValueHigh'}) }}

select claim_line_id, charge_amount, paid_amount, contractual_adjustment,
       deductible_amount, coinsurance_amount, other_adjustment
from {{ ref('remittance_line') }}
where charge_amount - contractual_adjustment - deductible_amount - coinsurance_amount
      - other_adjustment <> paid_amount
