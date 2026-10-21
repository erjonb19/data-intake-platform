-- MIMIC-IV labevents -> staging contract. Lab items are MIMIC's own codes (itemid, no
-- LOINC in this release): vocabulary LOCAL. Text-only results keep their text.
select
    labevent_id::text as observation_id,
    0 as component_index,
    subject_id::text as patient_id,
    hadm_id::text as encounter_id,
    'laboratory' as category,
    itemid::text as source_code,
    'LOCAL' as source_vocabulary_id,
    {{ local_to_utc('charttime') }} as effective_at,
    valuenum as value_number,
    valueuom as unit_code,
    null::text as value_snomed_code,
    case when valuenum is null then value end as value_string
from {{ source('mimic', 'labevents') }}
