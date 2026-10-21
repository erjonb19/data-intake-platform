-- Staging contract (see _staging.yml): every source produces these columns.
select
    patient_id, source_identifier, gender, birth_date, deceased_at, race_code, ethnicity_code
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
from {{ ref('mimic_patient') }}
{% else %}
from {{ ref('fhir_patient') }}
{% endif %}
