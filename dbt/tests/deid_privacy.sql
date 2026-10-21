-- Privacy invariants of the shareable tier (R4). Returns one row per violation.
{{ config(meta={'dq_category': 'privacy'}) }}

{% set ref_year = var('deid_reference_date')[:4] | int %}

-- 1. No restricted identifier reappears: pseudonyms differ from restricted ids.
select 'person id reused' as violation, p.person_id::text as detail
from {{ ref('share_person') }} as p
join {{ ref('person') }} as r on r.person_id = p.person_id
union all
select 'visit id reused', v.visit_occurrence_id::text
from {{ ref('share_visit_occurrence') }} as v
join {{ ref('visit_occurrence') }} as r on r.visit_occurrence_id = v.visit_occurrence_id

-- 2. No source identifiers or free text; birth is a year only; ages 90+ top-coded.
union all
select 'source identifier present', person_id::text
from {{ ref('share_person') }}
where person_source_value is not null
union all
select 'free text present', observation_id::text
from {{ ref('share_observation') }}
where value_as_string is not null
union all
select 'birth detail present', person_id::text
from {{ ref('share_person') }}
where month_of_birth is not null or day_of_birth is not null or birth_datetime is not null
union all
select 'age over 90 not top-coded', person_id::text
from {{ ref('share_person') }}
where year_of_birth < {{ ref_year - 90 }}

-- 3. Every person is shifted by 1 to 180 days, never 0.
union all
select 'shift out of range', person_id::text
from {{ ref('deid_person_map') }}
where shift_days = 0 or abs(shift_days) > 180

-- 4. Intervals are preserved: each person's observation period has the same length.
union all
select 'observation period length changed', m.pseudo_person_id::text
from {{ ref('observation_period') }} as r
join {{ ref('deid_person_map') }} as m on m.person_id = r.person_id
join {{ ref('share_observation_period') }} as s on s.person_id = m.pseudo_person_id
where (s.observation_period_end_date - s.observation_period_start_date)
    <> (r.observation_period_end_date - r.observation_period_start_date)

-- 5. Nothing is lost: same row counts in both tiers.
union all
select 'row count differs: ' || t.name, null
from (
    select 'measurement' as name,
        (select count(*) from {{ ref('measurement') }}) as restricted,
        (select count(*) from {{ ref('share_measurement') }}) as shareable
    union all
    select 'condition_occurrence',
        (select count(*) from {{ ref('condition_occurrence') }}),
        (select count(*) from {{ ref('share_condition_occurrence') }})
    union all
    select 'drug_exposure',
        (select count(*) from {{ ref('drug_exposure') }}),
        (select count(*) from {{ ref('share_drug_exposure') }})
) as t
where t.restricted <> t.shareable
