-- Staging contract (see _staging.yml): every source produces these columns.
select
    condition_id, patient_id, encounter_id, source_code, source_vocabulary_id,
    clinical_status, onset_at, abatement_at
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
from {{ ref('mimic_condition') }}
{% else %}
from {{ ref('fhir_condition') }}
{% endif %}
