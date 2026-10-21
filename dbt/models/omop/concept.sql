-- OMOP CONCEPT: the Eunomia shard plus the few fixed standard concepts it lacks.
select
    concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
    standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason
from {{ source('vocab', 'concept') }}
union all
select
    f.concept_id, f.concept_name, f.domain_id, f.vocabulary_id, f.concept_class_id,
    f.standard_concept, f.concept_code, f.valid_start_date, f.valid_end_date, f.invalid_reason
from {{ ref('omop_fixed_concepts') }} as f
where not exists (
    select 1 from {{ source('vocab', 'concept') }} as c where c.concept_id = f.concept_id
)
