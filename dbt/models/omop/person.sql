with demo as (select source_kind, source_code, concept_id from {{ ref('fhir_concept_map') }})

select
    {{ omop_id('p.patient_id') }} as person_id,
    coalesce(g.concept_id, 0) as gender_concept_id,
    extract(year from p.birth_date)::integer as year_of_birth,
    extract(month from p.birth_date)::integer as month_of_birth,
    extract(day from p.birth_date)::integer as day_of_birth,
    p.birth_date::timestamp as birth_datetime,
    coalesce(r.concept_id, 0) as race_concept_id,
    coalesce(e.concept_id, 0) as ethnicity_concept_id,
    null::bigint as location_id,
    null::bigint as provider_id,
    null::bigint as care_site_id,
    p.patient_id::varchar(50) as person_source_value,
    p.gender::varchar(50) as gender_source_value,
    0 as gender_source_concept_id,
    p.race_code::varchar(50) as race_source_value,
    0 as race_source_concept_id,
    p.ethnicity_code::varchar(50) as ethnicity_source_value,
    0 as ethnicity_source_concept_id
from {{ ref('fhir_patient') }} as p
left join demo as g on g.source_kind = 'gender' and g.source_code = p.gender
left join demo as r on r.source_kind = 'race' and r.source_code = p.race_code
left join demo as e on e.source_kind = 'ethnicity' and e.source_code = p.ethnicity_code
