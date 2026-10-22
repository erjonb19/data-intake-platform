select
    {{ omop_id('v.encounter_id') }} as visit_occurrence_id,
    {{ omop_id('v.patient_id') }} as person_id,
    coalesce(m.concept_id, 0) as visit_concept_id,
    v.start_at::date as visit_start_date,
    v.start_at as visit_start_datetime,
    coalesce(v.end_at, v.start_at)::date as visit_end_date,
    coalesce(v.end_at, v.start_at) as visit_end_datetime,
    {{ ehr_type_concept() }} as visit_type_concept_id,
    null::bigint as provider_id,
    null::bigint as care_site_id,
    v.class_code::varchar(50) as visit_source_value,
    0 as visit_source_concept_id,
    0 as admitted_from_concept_id,
    null::varchar(50) as admitted_from_source_value,
    0 as discharged_to_concept_id,
    null::varchar(50) as discharged_to_source_value,
    null::bigint as preceding_visit_occurrence_id
from {{ ref('fhir_encounter') }} as v
left join {{ ref('fhir_concept_map') }} as m
    on m.source_kind = 'encounter_class' and m.source_code = v.class_code
