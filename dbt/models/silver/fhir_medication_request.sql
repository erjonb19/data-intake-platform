-- Orders are coded inline (medicationCodeableConcept) or through a Medication resource
-- (medicationReference). RxNorm is preferred (Synthea); otherwise NDC, then the source's own
-- medication code (MIMIC). IV mixes have no code at all, only ingredients: their source
-- value is the mix's identifier, so the row stays traceable.
{% set med_systems = [
    'http://www.nlm.nih.gov/research/umls/rxnorm',
    'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-medication-ndc',
] %}
with requests as (
    select *
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'MedicationRequest'
),

medications as (
    select
        resource_id as medication_id,
        resource -> 'code' as code,
        resource -> 'identifier' -> 0 ->> 'value' as identifier
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Medication'
),

picked as (
    select
        r.*,
        coalesce(
            {{ fhir_pick_coding("r.resource -> 'medicationCodeableConcept'", med_systems) }},
            {{ fhir_pick_coding('m.code', med_systems) }}
        ) as coding,
        m.identifier as medication_identifier
    from requests as r
    left join medications as m
        on m.medication_id = {{ fhir_ref_id("r.resource -> 'medicationReference' ->> 'reference'") }}
)

select
    resource_id as medication_request_id,
    patient_id,
    {{ fhir_ref_id("resource -> 'encounter' ->> 'reference'") }} as encounter_id,
    resource ->> 'status' as status,
    coalesce(coding ->> 'code', medication_identifier) as source_code,
    {{ fhir_system_to_vocabulary("coding ->> 'system'") }} as source_vocabulary_id,
    coding ->> 'display' as display,
    {{ fhir_ts("resource ->> 'authoredOn'") }} as authored_at
from picked
