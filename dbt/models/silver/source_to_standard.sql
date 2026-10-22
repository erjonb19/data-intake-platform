-- Needed by every source, so always enabled.
{{ config(materialized='table', enabled=true) }}

-- Source code -> OMOP source concept and standard concept, via "Maps to".
-- One row per (vocabulary_id, concept_code); if a code maps to several standard concepts,
-- the lowest concept_id wins so the result is deterministic.
select distinct on (src.vocabulary_id, src.concept_code)
    src.vocabulary_id,
    src.concept_code,
    src.concept_id as source_concept_id,
    coalesce(std.concept_id, case when src.standard_concept = 'S' then src.concept_id end)
        as standard_concept_id,
    coalesce(std.domain_id, case when src.standard_concept = 'S' then src.domain_id end)
        as standard_domain_id
from {{ source('vocab', 'concept') }} as src
left join {{ source('vocab', 'concept_relationship') }} as cr
    on cr.concept_id_1 = src.concept_id
    and cr.relationship_id = 'Maps to'
    and cr.invalid_reason is null
left join {{ source('vocab', 'concept') }} as std
    on std.concept_id = cr.concept_id_2
    and std.standard_concept = 'S'
    and std.invalid_reason is null
order by
    src.vocabulary_id, src.concept_code,
    std.concept_id is null, src.invalid_reason is not null, std.concept_id
