-- Staging contract (see _staging.yml): every source produces these columns.
select
    medication_request_id, patient_id, encounter_id, source_code, source_vocabulary_id,
    authored_at
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
from {{ ref('mimic_medication_request') }}
{% else %}
from {{ ref('fhir_medication_request') }}
{% endif %}
