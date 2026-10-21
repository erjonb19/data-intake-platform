select
    medication_request_id, patient_id, encounter_id, source_code, source_vocabulary_id,
    authored_at
from {{ ref('fhir_medication_request') }}
