-- Finance (S2.5): an invoice's total is its subtotal plus tax, to the cent. The contract
-- cannot check across columns, so a register with a wrong total loads and this finds it.
-- A warning: AP works the exception with the vendor; the invoice still exists.
{{ config(severity='warn', tags=['finance.vendor_invoice'],
          meta={'dq_category': 'conformance', 'dqd_check': 'crossColumnSum'}) }}

select invoice_number, subtotal, tax, total
from {{ ref('vendor_invoice') }}
where abs(subtotal + tax - total) > 0.005
