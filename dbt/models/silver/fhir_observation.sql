-- One row per observed value: the Observation's own value (component_index 0), plus one
-- row per component (for example systolic and diastolic in a blood pressure panel).
-- The code is LOINC when present (Synthea), else the source's own code (MIMIC lab items).
with obs as (
    select *
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Observation'
),

own_value as (
    select
        resource_id as observation_id,
        0 as component_index,
        patient_id,
        resource,
        resource -> 'code' as code,
        resource -> 'valueQuantity' as value_quantity,
        resource -> 'valueCodeableConcept' as value_concept,
        resource ->> 'valueString' as value_string
    from obs
    where resource ? 'valueQuantity' or resource ? 'valueCodeableConcept'
        or not resource ? 'component'
),

components as (
    select
        obs.resource_id as observation_id,
        c.ordinality::integer as component_index,
        obs.patient_id,
        obs.resource,
        c.component -> 'code' as code,
        c.component -> 'valueQuantity' as value_quantity,
        c.component -> 'valueCodeableConcept' as value_concept,
        c.component ->> 'valueString' as value_string
    from obs,
        jsonb_array_elements(obs.resource -> 'component') with ordinality as c (component, ordinality)
    where jsonb_typeof(obs.resource -> 'component') = 'array'
),

picked as (
    select
        v.*,
        {{ fhir_pick_coding('v.code', ['http://loinc.org']) }} as coding
    from (select * from own_value union all select * from components) as v
)

select
    observation_id,
    component_index,
    patient_id,
    {{ fhir_ref_id("resource -> 'encounter' ->> 'reference'") }} as encounter_id,
    resource -> 'category' -> 0 -> 'coding' -> 0 ->> 'code' as category,
    resource ->> 'status' as status,
    coding ->> 'code' as source_code,
    {{ fhir_system_to_vocabulary("coding ->> 'system'") }} as source_vocabulary_id,
    coalesce(coding ->> 'display', code ->> 'text') as display,
    {{ fhir_ts("resource ->> 'effectiveDateTime'") }} as effective_at,
    (value_quantity ->> 'value')::numeric as value_number,
    coalesce(value_quantity ->> 'code', value_quantity ->> 'unit') as unit_code,
    {{ fhir_code('value_concept', 'http://snomed.info/sct') }} as value_snomed_code,
    value_string
from picked
