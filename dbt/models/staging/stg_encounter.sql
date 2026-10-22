-- visit_concept_id: from the encounter's identifier system when the source defines one
-- (MIMIC: a hospital encounter is inpatient whatever its class, which carries the admission
-- type; an ED encounter is an ER visit), else from Encounter.class. ICU encounters are OMOP
-- visit_detail, which is not built, so they are not visits: their events attach to the
-- hospital encounter they are part of (visit_encounter_id).
{% if var('source_kind', 'fhir') == 'mimic_relational' %}
-- The relational source already resolves visit type and visit membership.
select
    encounter_id, patient_id, class_code, source_identifier, start_at, end_at,
    visit_concept_id, is_visit, visit_encounter_id
from {{ ref('mimic_encounter') }}
{% else %}
with e as (select * from {{ ref('fhir_encounter') }}),
m as (select source_kind, source_code, concept_id from {{ ref('fhir_concept_map') }})

select
    e.encounter_id,
    e.patient_id,
    e.class_code,
    e.source_identifier,
    e.start_at,
    e.end_at,
    coalesce(mp.concept_id, mc.concept_id, 0) as visit_concept_id,
    coalesce(e.identifier_system not like '%/encounter-icu', true) as is_visit,
    case
        when e.identifier_system like '%/encounter-icu' and e.parent_encounter_id is not null
            then e.parent_encounter_id
        else e.encounter_id
    end as visit_encounter_id
from e
left join m as mp
    on mp.source_kind = 'encounter_identifier_system' and mp.source_code = e.identifier_system
left join m as mc on mc.source_kind = 'encounter_class' and mc.source_code = e.class_code
{% endif %}
