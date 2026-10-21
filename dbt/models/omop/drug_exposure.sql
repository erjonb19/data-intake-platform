-- From MedicationRequest (prescriptions written). The orders carry no end date, so the
-- end date equals the start date, the usual OMOP convention when it is unknown.
select
    {{ omop_id('m.medication_request_id') }} as drug_exposure_id,
    {{ omop_id('m.patient_id') }} as person_id,
    coalesce(s.standard_concept_id, 0) as drug_concept_id,
    m.authored_at::date as drug_exposure_start_date,
    m.authored_at as drug_exposure_start_datetime,
    m.authored_at::date as drug_exposure_end_date,
    m.authored_at as drug_exposure_end_datetime,
    null::date as verbatim_end_date,
    {{ ehr_type_concept() }} as drug_type_concept_id,
    null::varchar(20) as stop_reason,
    null::integer as refills,
    null::numeric as quantity,
    null::integer as days_supply,
    null::text as sig,
    0 as route_concept_id,
    null::varchar(50) as lot_number,
    null::bigint as provider_id,
    case when ve.visit_encounter_id is not null then {{ omop_id('ve.visit_encounter_id') }} end
        as visit_occurrence_id,
    null::bigint as visit_detail_id,
    m.source_code::varchar(50) as drug_source_value,
    coalesce(s.source_concept_id, 0) as drug_source_concept_id,
    null::varchar(50) as route_source_value,
    null::varchar(50) as dose_unit_source_value
from {{ ref('stg_medication_request') }} as m
left join {{ ref('stg_encounter') }} as ve on ve.encounter_id = m.encounter_id
left join {{ ref('source_to_standard') }} as s
    on s.vocabulary_id = m.source_vocabulary_id and s.concept_code = m.source_code
