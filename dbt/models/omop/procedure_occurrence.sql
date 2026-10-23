-- From FHIR Procedure (R7: the cohort service's procedure criteria read this table).
-- Only Procedure-domain concepts. Some SNOMED procedures have a standard concept in another
-- domain (for example a throat culture, domain Measurement): OMOP would move those rows to
-- that table, which this build does not do yet. They stay here with concept 0 and keep
-- their source concept, so they are counted, not dropped (PLAN.md follow-ups).
select
    {{ omop_id('p.procedure_id') }} as procedure_occurrence_id,
    {{ omop_id('p.patient_id') }} as person_id,
    case when s.standard_domain_id = 'Procedure' then s.standard_concept_id else 0 end
        as procedure_concept_id,
    p.performed_at::date as procedure_date,
    p.performed_at as procedure_datetime,
    p.performed_end_at::date as procedure_end_date,
    p.performed_end_at as procedure_end_datetime,
    {{ ehr_type_concept() }} as procedure_type_concept_id,
    0 as modifier_concept_id,
    null::integer as quantity,
    null::bigint as provider_id,
    case when ve.visit_encounter_id is not null then {{ omop_id('ve.visit_encounter_id') }} end
        as visit_occurrence_id,
    null::bigint as visit_detail_id,
    p.source_code::varchar(50) as procedure_source_value,
    coalesce(s.source_concept_id, 0) as procedure_source_concept_id,
    null::varchar(50) as modifier_source_value
from {{ ref('stg_procedure') }} as p
left join {{ ref('stg_encounter') }} as ve on ve.encounter_id = p.encounter_id
left join {{ ref('source_to_standard') }} as s
    on s.vocabulary_id = p.source_vocabulary_id and s.concept_code = p.source_code
