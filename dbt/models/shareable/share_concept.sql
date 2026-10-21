{{ config(alias='concept') }}

-- The vocabulary is public reference data; researchers need it to read concept ids.
select * from {{ ref('concept') }}
