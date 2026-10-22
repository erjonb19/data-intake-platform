-- Finance (S2.5): an invoice is due on or after the day it was issued.
{{ config(severity='warn', tags=['finance.vendor_invoice'],
          meta={'dq_category': 'plausibility', 'dqd_check': 'plausibleStartBeforeEnd'}) }}

select invoice_number, invoice_date, due_date
from {{ ref('vendor_invoice') }}
where due_date < invoice_date
