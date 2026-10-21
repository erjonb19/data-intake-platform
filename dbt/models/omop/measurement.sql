-- OMOP routes records by the domain of their standard concept. Observations whose LOINC
-- code maps to the Measurement domain land here; unmapped ones land here too when FHIR
-- marks them as laboratory or vital-signs (concept_id 0, counted by the DQ checks).
-- Other FHIR Observations (surveys, social history) belong in OMOP OBSERVATION, which is
-- not built yet.
select
    {{ omop_id("o.observation_id || '#' || o.component_index") }} as measurement_id,
    {{ omop_id('o.patient_id') }} as person_id,
    coalesce(s.standard_concept_id, 0) as measurement_concept_id,
    o.effective_at::date as measurement_date,
    o.effective_at as measurement_datetime,
    null::varchar(10) as measurement_time,
    {{ ehr_type_concept() }} as measurement_type_concept_id,
    0 as operator_concept_id,
    o.value_number as value_as_number,
    coalesce(vs.standard_concept_id, 0) as value_as_concept_id,
    coalesce(u.standard_concept_id, 0) as unit_concept_id,
    null::numeric as range_low,
    null::numeric as range_high,
    null::bigint as provider_id,
    case when o.encounter_id is not null then {{ omop_id('o.encounter_id') }} end
        as visit_occurrence_id,
    null::bigint as visit_detail_id,
    o.loinc_code::varchar(50) as measurement_source_value,
    coalesce(s.source_concept_id, 0) as measurement_source_concept_id,
    o.unit_code::varchar(50) as unit_source_value,
    0 as unit_source_concept_id,
    coalesce(o.value_snomed_code, o.value_number::text)::varchar(50) as value_source_value,
    null::bigint as measurement_event_id,
    0 as meas_event_field_concept_id
from {{ ref('fhir_observation') }} as o
left join {{ ref('source_to_standard') }} as s
    on s.vocabulary_id = 'LOINC' and s.concept_code = o.loinc_code
left join {{ ref('source_to_standard') }} as u
    on u.vocabulary_id = 'UCUM' and u.concept_code = o.unit_code
left join {{ ref('source_to_standard') }} as vs
    on vs.vocabulary_id = 'SNOMED' and vs.concept_code = o.value_snomed_code
where s.standard_domain_id = 'Measurement'
    or (s.standard_concept_id is null and o.category in ('laboratory', 'vital-signs'))
