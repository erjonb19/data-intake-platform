-- One row per encounter that is a visit (ICU stays are visit_detail in OMOP; not built).
-- visit_concept_id comes from staging: AMB 9202 outpatient, EMER 9203 ER, IMP/ACUTE 9201
-- inpatient, MIMIC hospital encounters 9201 and MIMIC ED encounters 9203; others 0.
select
    {{ omop_id('v.encounter_id') }} as visit_occurrence_id,
    {{ omop_id('v.patient_id') }} as person_id,
    v.visit_concept_id,
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
from {{ ref('stg_encounter') }} as v
where v.is_visit
