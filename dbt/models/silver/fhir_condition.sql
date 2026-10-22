-- Conditions: SNOMED preferred (Synthea), then ICD-10-CM, ICD-10, ICD-9-CM (Synthea codes a
-- few in ICD-10 only; MIMIC codes everything in ICD-9/10 under its own system URIs).
-- MIMIC diagnoses carry no onset: they belong to a hospital stay, so the onset falls back to
-- the recorded date, then the start of the encounter (the usual OMOP convention).
{% set dx_systems = [
    'http://snomed.info/sct',
    'http://hl7.org/fhir/sid/icd-10-cm',
    'http://hl7.org/fhir/sid/icd-10',
    'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-diagnosis-icd10',
    'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-diagnosis-icd9',
] %}
with conditions as (
    select
        *,
        {{ fhir_pick_coding("resource -> 'code'", dx_systems) }} as coding,
        {{ fhir_ref_id("resource -> 'encounter' ->> 'reference'") }} as encounter_id
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Condition'
),

coded as (
    select
        *,
        {{ fhir_system_to_vocabulary("coding ->> 'system'") }} as vocabulary_id
    from conditions
)

select
    c.resource_id as condition_id,
    c.patient_id,
    c.encounter_id,
    {{ icd_with_dot("c.coding ->> 'code'", 'c.vocabulary_id') }} as source_code,
    c.vocabulary_id as source_vocabulary_id,
    coalesce(c.coding ->> 'display', c.resource -> 'code' ->> 'text') as display,
    c.resource -> 'clinicalStatus' -> 'coding' -> 0 ->> 'code' as clinical_status,
    coalesce(
        {{ fhir_ts("c.resource ->> 'onsetDateTime'") }},
        {{ fhir_ts("c.resource ->> 'recordedDate'") }},
        e.start_at
    ) as onset_at,
    {{ fhir_ts("c.resource ->> 'abatementDateTime'") }} as abatement_at
from coded as c
left join {{ ref('fhir_encounter') }} as e on e.encounter_id = c.encounter_id
