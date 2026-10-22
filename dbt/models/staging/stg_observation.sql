-- Staging contract (see _staging.yml): every source produces these columns.
select
    observation_id, component_index, patient_id, encounter_id, category,
    source_code, source_vocabulary_id, effective_at, value_number, unit_code,
    value_snomed_code, value_string
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
from {{ ref('mimic_observation') }}
{% else %}
from {{ ref('fhir_observation') }}
{% endif %}
