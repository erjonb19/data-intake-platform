-- Synthea codes most orders inline (medicationCodeableConcept) and some through a
-- Medication resource (medicationReference); both resolve to an RxNorm code here.
with requests as (
    select *
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'MedicationRequest'
),

medications as (
    select resource_id as medication_id, resource -> 'code' as code
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Medication'
)

select
    r.resource_id as medication_request_id,
    r.patient_id,
    {{ fhir_ref_id("r.resource -> 'encounter' ->> 'reference'") }} as encounter_id,
    r.resource ->> 'status' as status,
    coalesce(
        {{ fhir_code("r.resource -> 'medicationCodeableConcept'", 'http://www.nlm.nih.gov/research/umls/rxnorm') }},
        {{ fhir_code('m.code', 'http://www.nlm.nih.gov/research/umls/rxnorm') }}
    ) as rxnorm_code,
    coalesce(
        {{ fhir_code("r.resource -> 'medicationCodeableConcept'", 'http://www.nlm.nih.gov/research/umls/rxnorm', 'display') }},
        {{ fhir_code('m.code', 'http://www.nlm.nih.gov/research/umls/rxnorm', 'display') }}
    ) as display,
    {{ fhir_ts("r.resource ->> 'authoredOn'") }} as authored_at
from requests as r
left join medications as m
    on m.medication_id = {{ fhir_ref_id("r.resource -> 'medicationReference' ->> 'reference'") }}
