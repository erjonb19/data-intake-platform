{{ config(alias='observation') }}

-- Free-text values (value_as_string, and value_source_value when it holds text) are
-- dropped: free text can carry identifiers.
select
    {{ deid_id('o.observation_id', 'observation') }} as observation_id,
    m.pseudo_person_id as person_id,
    o.observation_concept_id,
    {{ shift_date('o.observation_date') }} as observation_date,
    {{ shift_ts('o.observation_datetime') }} as observation_datetime,
    o.observation_type_concept_id,
    o.value_as_number,
    null::varchar(60) as value_as_string,
    o.value_as_concept_id,
    o.qualifier_concept_id,
    o.unit_concept_id,
    null::bigint as provider_id,
    case when o.visit_occurrence_id is not null
        then {{ deid_id('o.visit_occurrence_id', 'visit') }} end as visit_occurrence_id,
    null::bigint as visit_detail_id,
    o.observation_source_value,
    o.observation_source_concept_id,
    o.unit_source_value,
    null::varchar(50) as qualifier_source_value,
    case when o.value_as_string is null then o.value_source_value end as value_source_value,
    null::bigint as observation_event_id,
    o.obs_event_field_concept_id
from {{ ref('observation') }} as o
join {{ ref('deid_person_map') }} as m on m.person_id = o.person_id
