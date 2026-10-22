-- One observation period per person, from their first to their last recorded event in
-- any clinical table. Longitudinal questions ("2+ HbA1c in the last year") only make
-- sense inside this window.
with events as (
    select person_id, visit_start_date as start_date, visit_end_date as end_date
    from {{ ref('visit_occurrence') }}
    union all
    select person_id, condition_start_date, coalesce(condition_end_date, condition_start_date)
    from {{ ref('condition_occurrence') }}
    union all
    select person_id, measurement_date, measurement_date
    from {{ ref('measurement') }}
    union all
    select person_id, drug_exposure_start_date, drug_exposure_end_date
    from {{ ref('drug_exposure') }}
)

select
    person_id as observation_period_id,
    person_id,
    min(start_date) as observation_period_start_date,
    max(greatest(start_date, end_date)) as observation_period_end_date,
    {{ ehr_type_concept() }} as period_type_concept_id
from events
where person_id in (select person_id from {{ ref('person') }})
group by person_id
