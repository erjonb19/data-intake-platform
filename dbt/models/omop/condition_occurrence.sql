-- OMOP routes by domain: conditions whose standard concept is in another domain (for
-- example SNOMED situations and social findings, domain Observation) go to `observation`.
-- Unmapped codes stay here with concept_id 0.
select
    {{ omop_id('c.condition_id') }} as condition_occurrence_id,
    {{ omop_id('c.patient_id') }} as person_id,
    coalesce(s.standard_concept_id, 0) as condition_concept_id,
    c.onset_at::date as condition_start_date,
    c.onset_at as condition_start_datetime,
    c.abatement_at::date as condition_end_date,
    c.abatement_at as condition_end_datetime,
    {{ ehr_type_concept() }} as condition_type_concept_id,
    0 as condition_status_concept_id,
    null::varchar(20) as stop_reason,
    null::bigint as provider_id,
    case when ve.visit_encounter_id is not null then {{ omop_id('ve.visit_encounter_id') }} end
        as visit_occurrence_id,
    null::bigint as visit_detail_id,
    c.source_code::varchar(50) as condition_source_value,
    coalesce(s.source_concept_id, 0) as condition_source_concept_id,
    c.clinical_status::varchar(50) as condition_status_source_value
from {{ ref('stg_condition') }} as c
left join {{ ref('stg_encounter') }} as ve on ve.encounter_id = c.encounter_id
left join {{ ref('source_to_standard') }} as s
    on s.vocabulary_id = c.source_vocabulary_id and s.concept_code = c.source_code
where coalesce(s.standard_domain_id, 'Condition') = 'Condition'
