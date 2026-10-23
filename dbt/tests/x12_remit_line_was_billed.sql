-- 835 (S4.3): every remitted line matches a submitted 837P line (REF*6R). A warning: the
-- 837 may not have arrived yet, or was submitted through another clearinghouse.
{{ config(severity='warn', tags=['healthcare.remittance_835', 'healthcare.claims_837'],
          meta={'dq_category': 'completeness', 'dqd_check': 'isForeignKey'}) }}

select r.claim_line_id
from {{ ref('remittance_line') }} as r
left join {{ ref('professional_claim_line') }} as c on c.claim_line_id = r.claim_line_id
where c.claim_line_id is null
