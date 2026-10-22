{{ config(alias='procedure_occurrence') }}

select
    {{ deid_id('p.procedure_occurrence_id', 'procedure') }} as procedure_occurrence_id,
    m.pseudo_person_id as person_id,
    p.procedure_concept_id,
    {{ shift_date('p.procedure_date') }} as procedure_date,
    {{ shift_ts('p.procedure_datetime') }} as procedure_datetime,
    {{ shift_date('p.procedure_end_date') }} as procedure_end_date,
    {{ shift_ts('p.procedure_end_datetime') }} as procedure_end_datetime,
    p.procedure_type_concept_id,
    p.modifier_concept_id,
    p.quantity,
    null::bigint as provider_id,
    case when p.visit_occurrence_id is not null
        then {{ deid_id('p.visit_occurrence_id', 'visit') }} end as visit_occurrence_id,
    null::bigint as visit_detail_id,
    p.procedure_source_value,
    p.procedure_source_concept_id,
    null::varchar(50) as modifier_source_value
from {{ ref('procedure_occurrence') }} as p
join {{ ref('deid_person_map') }} as m on m.person_id = p.person_id
