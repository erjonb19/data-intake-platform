-- Needed by every source, so always enabled.
{{ config(materialized='table', enabled=true) }}

-- Source code -> OMOP source concept and standard concept, via "Maps to".
-- One row per (vocabulary_id, concept_code); if a code maps to several standard concepts,
-- the lowest concept_id wins so the result is deterministic.
with from_vocabulary as (
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
),

-- Local codes (no concept of their own, so source_concept_id 0) mapped by a
-- SOURCE_TO_CONCEPT_MAP. A target that has since been deprecated follows its "Maps to";
-- a target missing from the loaded vocabulary stays unmapped.
from_map as (
    select distinct on (m.source_vocabulary_id, m.source_code)
        m.source_vocabulary_id as vocabulary_id,
        m.source_code as concept_code,
        0 as source_concept_id,
        coalesce(std.concept_id, case when t.standard_concept = 'S' then t.concept_id end)
            as standard_concept_id,
        coalesce(std.domain_id, case when t.standard_concept = 'S' then t.domain_id end)
            as standard_domain_id
    from {{ source('vocab', 'source_to_concept_map') }} as m
    inner join {{ source('vocab', 'concept') }} as t
        on t.concept_id = m.target_concept_id
    left join {{ source('vocab', 'concept_relationship') }} as cr
        on cr.concept_id_1 = t.concept_id
        and cr.relationship_id = 'Maps to'
        and cr.invalid_reason is null
        and t.standard_concept is distinct from 'S'
    left join {{ source('vocab', 'concept') }} as std
        on std.concept_id = cr.concept_id_2
        and std.standard_concept = 'S'
        and std.invalid_reason is null
    where m.invalid_reason is null
    order by m.source_vocabulary_id, m.source_code, std.concept_id is null, std.concept_id
)

select * from from_vocabulary
union all
select m.* from from_map as m
where not exists (
    select 1 from from_vocabulary as v
    where v.vocabulary_id = m.vocabulary_id and v.concept_code = m.concept_code
)
