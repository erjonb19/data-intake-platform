-- Claims (S2.4): on every line, paid <= allowed <= billed, and the member's share
-- (coinsurance + deductible) plus the payment does not exceed the allowed amount.
-- A warning: the payer's own adjudication data, not something the intake can correct.
{{ config(severity='warn', tags=['healthcare.claims'],
          meta={'dq_category': 'plausibility', 'dqd_check': 'plausibleValueHigh'}) }}

select claim_line_id, billed_amount, allowed_amount, paid_amount,
       coinsurance_amount, deductible_amount
from {{ ref('claim_line') }}
where paid_amount > allowed_amount
    or allowed_amount > billed_amount
    or paid_amount + coinsurance_amount + deductible_amount > allowed_amount + 0.01
