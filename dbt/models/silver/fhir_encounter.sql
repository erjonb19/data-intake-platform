select
    resource_id as encounter_id,
    patient_id,
    resource -> 'class' ->> 'code' as class_code,
    {{ fhir_code("resource -> 'type' -> 0", 'http://snomed.info/sct') }} as type_snomed_code,
    {{ fhir_code("resource -> 'type' -> 0", 'http://snomed.info/sct', 'display') }} as type_display,
    {{ fhir_ts("resource -> 'period' ->> 'start'") }} as start_at,
    {{ fhir_ts("resource -> 'period' ->> 'end'") }} as end_at
from {{ ref('fhir_resource_current') }}
where resource_type = 'Encounter'
