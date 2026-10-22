-- Conditions are coded in SNOMED; Synthea v4 codes a few in ICD-10 only. The SNOMED code
-- wins when both are present.
with conditions as (
    select
        *,
        {{ fhir_code("resource -> 'code'", 'http://snomed.info/sct') }} as snomed_code,
        {{ fhir_code("resource -> 'code'", 'http://hl7.org/fhir/sid/icd-10-cm') }} as icd10cm_code,
        {{ fhir_code("resource -> 'code'", 'http://hl7.org/fhir/sid/icd-10') }} as icd10_code
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Condition'
)

select
    resource_id as condition_id,
    patient_id,
    {{ fhir_ref_id("resource -> 'encounter' ->> 'reference'") }} as encounter_id,
    coalesce(snomed_code, icd10cm_code, icd10_code) as source_code,
    case
        when snomed_code is not null then 'SNOMED'
        when icd10cm_code is not null then 'ICD10CM'
        else 'ICD10'  -- WHO ICD-10, as Synthea labels it
    end as source_vocabulary_id,
    coalesce(
        {{ fhir_code("resource -> 'code'", 'http://snomed.info/sct', 'display') }},
        resource -> 'code' ->> 'text'
    ) as display,
    resource -> 'clinicalStatus' -> 'coding' -> 0 ->> 'code' as clinical_status,
    {{ fhir_ts("resource ->> 'onsetDateTime'") }} as onset_at,
    {{ fhir_ts("resource ->> 'abatementDateTime'") }} as abatement_at
from conditions
