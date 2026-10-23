-- Claims (S2.4): every claim line belongs to a member the eligibility feed knows, and its
-- service date falls inside that member's coverage. A warning, not an error: a claim
-- outside coverage is a finding for the payer to work, not a reason to reject the line.
-- Runs once gold.member_eligibility exists (the eligibility feed's DAG builds it). It is
-- looked up, not ref()'d: a ref would make dbt's eager test selection run this claims
-- test in the eligibility feed's builds too, before any claim exists.
-- depends_on: {{ ref('claim_line') }}
{{ config(severity='warn', tags=['healthcare.claims'],
          meta={'dq_category': 'conformance', 'dqd_check': 'fkDomain'}) }}
{% set eligibility = adapter.get_relation(database=target.database, schema='gold',
                                           identifier='member_eligibility') %}

{% if eligibility %}
select c.claim_line_id, c.member_id, c.service_date, e.coverage_start, e.coverage_end,
       case when e.member_id is null then 'member not in eligibility'
            else 'service date outside coverage' end as finding
from {{ ref('claim_line') }} as c
left join {{ eligibility }} as e on e.member_id = c.member_id
where e.member_id is null
    or c.service_date < e.coverage_start
    or c.service_date > coalesce(e.coverage_end, date '9999-12-31')
{% else %}
select null::text as claim_line_id where false
{% endif %}
