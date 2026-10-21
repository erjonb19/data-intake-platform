select
    condition_id, patient_id, encounter_id, source_code, source_vocabulary_id,
    clinical_status, onset_at, abatement_at
from {{ ref('fhir_condition') }}
