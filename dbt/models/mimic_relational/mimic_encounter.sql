-- MIMIC-IV admissions -> staging contract. Every admission is an inpatient stay (9201);
-- admission_type (EW EMER., URGENT, OBSERVATION ADMIT, ...) is kept as the source value.
-- The relational demo has no ED module (that is the separate MIMIC-IV-ED demo), and ICU
-- stays are part of their admission, so there are no other visits here.
-- Times are local Boston time without an offset: they are read as America/New_York and
-- stored in UTC, like the FHIR path (whose timestamps carry -04:00/-05:00).
select
    hadm_id::text as encounter_id,
    subject_id::text as patient_id,
    admission_type as class_code,
    hadm_id::text as source_identifier,
    {{ local_to_utc('admittime') }} as start_at,
    {{ local_to_utc('dischtime') }} as end_at,
    9201 as visit_concept_id,
    true as is_visit,
    hadm_id::text as visit_encounter_id
from {{ source('mimic', 'admissions') }}
