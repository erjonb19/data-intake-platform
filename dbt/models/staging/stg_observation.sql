select
    observation_id, component_index, patient_id, encounter_id, category,
    source_code, source_vocabulary_id, effective_at, value_number, unit_code,
    value_snomed_code, value_string
from {{ ref('fhir_observation') }}
