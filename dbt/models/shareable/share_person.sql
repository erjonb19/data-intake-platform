{{ config(alias='person') }}

-- De-identified PERSON: pseudonymous id, year of birth only (from the shifted birth date),
-- ages 90 and over top-coded, no source identifier.
{% set ref_date = "date '" ~ var('deid_reference_date') ~ "'" %}
select
    m.pseudo_person_id as person_id,
    p.gender_concept_id,
    case
        when extract(year from age({{ ref_date }}, {{ shift_ts('p.birth_datetime') }})) >= 90
            then extract(year from {{ ref_date }})::integer - 90
        else extract(year from {{ shift_ts('p.birth_datetime') }})::integer
    end as year_of_birth,
    null::integer as month_of_birth,
    null::integer as day_of_birth,
    null::timestamp as birth_datetime,
    p.race_concept_id,
    p.ethnicity_concept_id,
    null::bigint as location_id,
    null::bigint as provider_id,
    null::bigint as care_site_id,
    null::varchar(50) as person_source_value,
    p.gender_source_value,
    p.gender_source_concept_id,
    p.race_source_value,
    p.race_source_concept_id,
    p.ethnicity_source_value,
    p.ethnicity_source_concept_id
from {{ ref('person') }} as p
join {{ ref('deid_person_map') }} as m on m.person_id = p.person_id
