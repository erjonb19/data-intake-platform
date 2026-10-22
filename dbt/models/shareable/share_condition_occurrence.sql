{{ config(alias='condition_occurrence') }}

select
    {{ deid_id('c.condition_occurrence_id', 'condition') }} as condition_occurrence_id,
    m.pseudo_person_id as person_id,
    c.condition_concept_id,
    {{ shift_date('c.condition_start_date') }} as condition_start_date,
    {{ shift_ts('c.condition_start_datetime') }} as condition_start_datetime,
    {{ shift_date('c.condition_end_date') }} as condition_end_date,
    {{ shift_ts('c.condition_end_datetime') }} as condition_end_datetime,
    c.condition_type_concept_id,
    c.condition_status_concept_id,
    null::varchar(20) as stop_reason,
    null::bigint as provider_id,
    case when c.visit_occurrence_id is not null
        then {{ deid_id('c.visit_occurrence_id', 'visit') }} end as visit_occurrence_id,
    null::bigint as visit_detail_id,
    c.condition_source_value,
    c.condition_source_concept_id,
    c.condition_status_source_value
from {{ ref('condition_occurrence') }} as c
join {{ ref('deid_person_map') }} as m on m.person_id = c.person_id
