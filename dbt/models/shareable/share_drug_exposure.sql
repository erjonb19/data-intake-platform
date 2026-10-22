{{ config(alias='drug_exposure') }}

select
    {{ deid_id('d.drug_exposure_id', 'drug') }} as drug_exposure_id,
    m.pseudo_person_id as person_id,
    d.drug_concept_id,
    {{ shift_date('d.drug_exposure_start_date') }} as drug_exposure_start_date,
    {{ shift_ts('d.drug_exposure_start_datetime') }} as drug_exposure_start_datetime,
    {{ shift_date('d.drug_exposure_end_date') }} as drug_exposure_end_date,
    {{ shift_ts('d.drug_exposure_end_datetime') }} as drug_exposure_end_datetime,
    null::date as verbatim_end_date,
    d.drug_type_concept_id,
    null::varchar(20) as stop_reason,
    d.refills,
    d.quantity,
    d.days_supply,
    null::text as sig,
    d.route_concept_id,
    null::varchar(50) as lot_number,
    null::bigint as provider_id,
    case when d.visit_occurrence_id is not null
        then {{ deid_id('d.visit_occurrence_id', 'visit') }} end as visit_occurrence_id,
    null::bigint as visit_detail_id,
    d.drug_source_value,
    d.drug_source_concept_id,
    d.route_source_value,
    d.dose_unit_source_value
from {{ ref('drug_exposure') }} as d
join {{ ref('deid_person_map') }} as m on m.person_id = d.person_id
