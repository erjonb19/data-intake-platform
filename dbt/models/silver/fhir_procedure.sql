-- Procedures: SNOMED, or CDT for Synthea v4's dental procedures (CDT is not in the Eunomia
-- shard, so in CI those keep concept 0 and their source code). Synthea R4 records the time
-- as performedPeriod; other sources may use performedDateTime. Procedures that were not
-- done, or entered in error, did not happen, so OMOP gets neither.
{% set px_systems = ['http://snomed.info/sct', 'http://www.ada.org/cdt'] %}
with procedures as (
    select
        *,
        {{ fhir_pick_coding("resource -> 'code'", px_systems) }} as coding,
        {{ fhir_ref_id("resource -> 'encounter' ->> 'reference'") }} as encounter_id
    from {{ ref('fhir_resource_current') }}
    where resource_type = 'Procedure'
        and resource ->> 'status' is distinct from 'not-done'
        and resource ->> 'status' is distinct from 'entered-in-error'
)

select
    p.resource_id as procedure_id,
    p.patient_id,
    p.encounter_id,
    p.coding ->> 'code' as source_code,
    {{ fhir_system_to_vocabulary("p.coding ->> 'system'") }} as source_vocabulary_id,
    coalesce(
        {{ fhir_ts("p.resource -> 'performedPeriod' ->> 'start'") }},
        {{ fhir_ts("p.resource ->> 'performedDateTime'") }}
    ) as performed_at,
    {{ fhir_ts("p.resource -> 'performedPeriod' ->> 'end'") }} as performed_end_at
from procedures as p
