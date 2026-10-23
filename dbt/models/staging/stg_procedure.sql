-- Staging contract (see _staging.yml): every source produces these columns.
-- MIMIC relational procedures (procedures_icd, ICD-9/10-PCS) are not mapped yet, so that
-- source produces no rows here (PLAN.md follow-ups).
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
select
    null::text as procedure_id, null::text as patient_id, null::text as encounter_id,
    null::text as source_code, null::text as source_vocabulary_id,
    null::timestamp as performed_at, null::timestamp as performed_end_at
where false
{% else %}
select
    procedure_id, patient_id, encounter_id, source_code, source_vocabulary_id,
    performed_at, performed_end_at
from {{ ref('fhir_procedure') }}
{% endif %}
