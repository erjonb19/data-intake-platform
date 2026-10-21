-- MIMIC-IV labevents -> staging contract. Lab items are MIMIC's own codes (itemid, no
-- LOINC in this release): vocabulary MIMIC_LAB, mapped to LOINC through
-- vocab.source_to_concept_map when the mimic-code map is loaded. Text-only results keep
-- their text.
select
    labevent_id::text as observation_id,
    0 as component_index,
    subject_id::text as patient_id,
    hadm_id::text as encounter_id,
    'laboratory' as category,
    itemid::text as source_code,
    'MIMIC_LAB' as source_vocabulary_id,
    {{ local_to_utc('charttime') }} as effective_at,
    valuenum as value_number,
    -- Some units are a single space; blank means no unit (the FHIR export agrees).
    nullif(trim(valueuom), '') as unit_code,
    null::text as value_snomed_code,
    case when valuenum is null then value end as value_string
from {{ source('mimic', 'labevents') }}
