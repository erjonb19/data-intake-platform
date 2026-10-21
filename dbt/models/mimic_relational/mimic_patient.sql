-- MIMIC-IV patients -> staging contract. MIMIC gives anchor_age at anchor_year (both
-- shifted), not a birth date, so the birth date is year-only: January 1st of
-- anchor_year - anchor_age. Race and ethnicity are free text on each admission; the most
-- recent admission's value is mapped to a CDC OMB category where the text allows it.
with latest_race as (
    select distinct on (subject_id) subject_id, upper(race) as race
    from {{ source('mimic', 'admissions') }}
    order by subject_id, admittime desc, hadm_id desc
)

select
    p.subject_id::text as patient_id,
    p.subject_id::text as source_identifier,
    case p.gender when 'F' then 'female' when 'M' then 'male' else 'unknown' end as gender,
    make_date(p.anchor_year - p.anchor_age, 1, 1) as birth_date,
    p.dod::timestamp as deceased_at,
    case
        when r.race like 'WHITE%' or r.race = 'PORTUGUESE' then '2106-3'
        when r.race like 'BLACK%' then '2054-5'
        when r.race like 'ASIAN%' then '2028-9'
        when r.race like 'AMERICAN INDIAN%' then '1002-5'
        when r.race like 'NATIVE HAWAIIAN%' then '2076-8'
    end as race_code,
    case
        when r.race like 'HISPANIC%' or r.race like '%LATINO%' or r.race = 'SOUTH AMERICAN'
            then '2135-2'
        when r.race is null or r.race in ('UNKNOWN', 'UNABLE TO OBTAIN', 'PATIENT DECLINED TO ANSWER', 'OTHER')
            then null
        else '2186-5'
    end as ethnicity_code
from {{ source('mimic', 'patients') }} as p
left join latest_race as r on r.subject_id = p.subject_id
