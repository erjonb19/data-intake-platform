{{ config(alias='observation_period') }}

select
    {{ deid_id('o.observation_period_id', 'observation_period') }} as observation_period_id,
    m.pseudo_person_id as person_id,
    {{ shift_date('o.observation_period_start_date') }} as observation_period_start_date,
    {{ shift_date('o.observation_period_end_date') }} as observation_period_end_date,
    o.period_type_concept_id
from {{ ref('observation_period') }} as o
join {{ ref('deid_person_map') }} as m on m.person_id = o.person_id
