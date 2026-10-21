-- Every staged condition and observation lands in exactly one OMOP table: domain routing
-- moves rows between tables but never drops one. Returns a row per imbalance.
{{ config(meta={'dq_category': 'completeness'}) }}

with staged as (
    select
        (select count(*) from {{ ref('stg_condition') }}) as conditions,
        (select count(*) from {{ ref('stg_observation') }}) as observations
),

landed as (
    select
        (select count(*) from {{ ref('condition_occurrence') }})
        + (select count(*) from {{ ref('measurement') }})
        + (select count(*) from {{ ref('observation') }}) as rows_out
)

select s.conditions + s.observations as rows_in, l.rows_out
from staged as s cross join landed as l
where s.conditions + s.observations <> l.rows_out
