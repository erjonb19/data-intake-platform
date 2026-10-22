-- OMOP OBSERVATION: clinical facts whose standard concept is in the Observation domain.
-- Two sources: FHIR Conditions coded as SNOMED situations or social findings (for example
-- "Medication review due", employment status), and FHIR Observations whose LOINC code maps
-- to the Observation domain (for example tobacco smoking status, survey answers).
with from_conditions as (
    select
        'condition:' || c.condition_id as source_key,
        c.patient_id,
        c.encounter_id,
        s.standard_concept_id as concept_id,
        s.source_concept_id,
        c.onset_at as observed_at,
        null::numeric as value_number,
        null::text as value_string,
        null::text as value_snomed_code,
        null::text as unit_code,
        c.source_code
    from {{ ref('stg_condition') }} as c
    join {{ ref('source_to_standard') }} as s
        on s.vocabulary_id = c.source_vocabulary_id and s.concept_code = c.source_code
    where s.standard_domain_id = 'Observation'
),

from_observations as (
    select
        'observation:' || o.observation_id || '#' || o.component_index as source_key,
        o.patient_id,
        o.encounter_id,
        s.standard_concept_id,
        s.source_concept_id,
        o.effective_at,
        o.value_number,
        o.value_string,
        o.value_snomed_code,
        o.unit_code,
        o.source_code
    from {{ ref('stg_observation') }} as o
    join {{ ref('source_to_standard') }} as s
        on s.vocabulary_id = o.source_vocabulary_id and s.concept_code = o.source_code
    where s.standard_domain_id = 'Observation'
),

facts as (
    select * from from_conditions
    union all
    select * from from_observations
)

select
    {{ omop_id('f.source_key') }} as observation_id,
    {{ omop_id('f.patient_id') }} as person_id,
    f.concept_id as observation_concept_id,
    f.observed_at::date as observation_date,
    f.observed_at as observation_datetime,
    {{ ehr_type_concept() }} as observation_type_concept_id,
    f.value_number as value_as_number,
    f.value_string::varchar(60) as value_as_string,
    coalesce(vs.standard_concept_id, 0) as value_as_concept_id,
    0 as qualifier_concept_id,
    coalesce(u.standard_concept_id, 0) as unit_concept_id,
    null::bigint as provider_id,
    case when ve.visit_encounter_id is not null then {{ omop_id('ve.visit_encounter_id') }} end
        as visit_occurrence_id,
    null::bigint as visit_detail_id,
    f.source_code::varchar(50) as observation_source_value,
    f.source_concept_id as observation_source_concept_id,
    f.unit_code::varchar(50) as unit_source_value,
    null::varchar(50) as qualifier_source_value,
    coalesce(f.value_snomed_code, f.value_string, f.value_number::text)::varchar(50)
        as value_source_value,
    null::bigint as observation_event_id,
    0 as obs_event_field_concept_id
from facts as f
left join {{ ref('stg_encounter') }} as ve on ve.encounter_id = f.encounter_id
left join {{ ref('source_to_standard') }} as u
    on u.vocabulary_id = 'UCUM' and u.concept_code = f.unit_code
left join {{ ref('source_to_standard') }} as vs
    on vs.vocabulary_id = 'SNOMED' and vs.concept_code = f.value_snomed_code
