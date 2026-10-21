-- Encounters. `identifier_system` and `parent_encounter_id` (Encounter.partOf) matter for
-- MIMIC: its hospital, ED and ICU encounters share one profile and differ only in the
-- identifier system, and ED and ICU encounters are part of a hospital encounter.
select
    resource_id as encounter_id,
    patient_id,
    resource -> 'class' ->> 'code' as class_code,
    resource -> 'meta' -> 'profile' ->> 0 as profile,
    resource -> 'identifier' -> 0 ->> 'system' as identifier_system,
    resource -> 'identifier' -> 0 ->> 'value' as source_identifier,
    {{ fhir_ref_id("resource -> 'partOf' ->> 'reference'") }} as parent_encounter_id,
    {{ fhir_code("resource -> 'type' -> 0", 'http://snomed.info/sct') }} as type_snomed_code,
    {{ fhir_code("resource -> 'type' -> 0", 'http://snomed.info/sct', 'display') }} as type_display,
    {{ fhir_ts("resource -> 'period' ->> 'start'") }} as start_at,
    {{ fhir_ts("resource -> 'period' ->> 'end'") }} as end_at
from {{ ref('fhir_resource_current') }}
where resource_type = 'Encounter'
