{{ config(alias='measurement') }}

select
    {{ deid_id('x.measurement_id', 'measurement') }} as measurement_id,
    m.pseudo_person_id as person_id,
    x.measurement_concept_id,
    {{ shift_date('x.measurement_date') }} as measurement_date,
    {{ shift_ts('x.measurement_datetime') }} as measurement_datetime,
    null::varchar(10) as measurement_time,
    x.measurement_type_concept_id,
    x.operator_concept_id,
    x.value_as_number,
    x.value_as_concept_id,
    x.unit_concept_id,
    x.range_low,
    x.range_high,
    null::bigint as provider_id,
    case when x.visit_occurrence_id is not null
        then {{ deid_id('x.visit_occurrence_id', 'visit') }} end as visit_occurrence_id,
    null::bigint as visit_detail_id,
    x.measurement_source_value,
    x.measurement_source_concept_id,
    x.unit_source_value,
    x.unit_source_concept_id,
    x.value_source_value,
    null::bigint as measurement_event_id,
    x.meas_event_field_concept_id
from {{ ref('measurement') }} as x
join {{ ref('deid_person_map') }} as m on m.person_id = x.person_id
