-- MIMIC-IV diagnoses_icd -> staging contract. A diagnosis belongs to an admission and has
-- no date of its own: it takes the admission start, as on the FHIR path.
select
    d.hadm_id::text || '-' || d.seq_num::text as condition_id,
    d.subject_id::text as patient_id,
    d.hadm_id::text as encounter_id,
    {{ icd_with_dot('d.icd_code', "case d.icd_version when 9 then 'ICD9CM' else 'ICD10CM' end") }}
        as source_code,
    case d.icd_version when 9 then 'ICD9CM' else 'ICD10CM' end as source_vocabulary_id,
    null::text as clinical_status,
    {{ local_to_utc('a.admittime') }} as onset_at,
    null::timestamp as abatement_at
from {{ source('mimic', 'diagnoses_icd') }} as d
join {{ source('mimic', 'admissions') }} as a on a.hadm_id = d.hadm_id
