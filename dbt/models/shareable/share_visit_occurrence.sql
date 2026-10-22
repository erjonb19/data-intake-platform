{{ config(alias='visit_occurrence') }}

select
    {{ deid_id('v.visit_occurrence_id', 'visit') }} as visit_occurrence_id,
    m.pseudo_person_id as person_id,
    v.visit_concept_id,
    {{ shift_date('v.visit_start_date') }} as visit_start_date,
    {{ shift_ts('v.visit_start_datetime') }} as visit_start_datetime,
    {{ shift_date('v.visit_end_date') }} as visit_end_date,
    {{ shift_ts('v.visit_end_datetime') }} as visit_end_datetime,
    v.visit_type_concept_id,
    null::bigint as provider_id,
    null::bigint as care_site_id,
    v.visit_source_value,
    v.visit_source_concept_id,
    v.admitted_from_concept_id,
    null::varchar(50) as admitted_from_source_value,
    v.discharged_to_concept_id,
    null::varchar(50) as discharged_to_source_value,
    null::bigint as preceding_visit_occurrence_id
from {{ ref('visit_occurrence') }} as v
join {{ ref('deid_person_map') }} as m on m.person_id = v.person_id
