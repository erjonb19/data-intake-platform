-- Privacy invariants of the shareable tier (R4). Returns one row per violation.
{{ config(meta={'dq_category': 'privacy'}) }}

{% set ref_year = var('deid_reference_date')[:4] | int %}

-- 1. No restricted identifier reappears: pseudonyms differ from restricted ids.
select 'person id reused' as violation, p.person_id::text as detail
from {{ ref('share_person') }} as p
join {{ ref('person') }} as r on r.person_id = p.person_id
union all
select 'visit id reused', v.visit_occurrence_id::text
from {{ ref('share_visit_occurrence') }} as v
join {{ ref('visit_occurrence') }} as r on r.visit_occurrence_id = v.visit_occurrence_id

-- 2. No source identifiers or free text; birth is a year only; ages 90+ top-coded.
union all
select 'source identifier present', person_id::text
from {{ ref('share_person') }}
where person_source_value is not null
union all
select 'free text present', observation_id::text
from {{ ref('share_observation') }}
where value_as_string is not null
union all
select 'birth detail present', person_id::text
from {{ ref('share_person') }}
where month_of_birth is not null or day_of_birth is not null or birth_datetime is not null
union all
select 'age over 90 not top-coded', person_id::text
from {{ ref('share_person') }}
where year_of_birth < {{ ref_year - 90 }}

-- 3. Every person is shifted by 1 to 180 days, never 0.
union all
select 'shift out of range', person_id::text
from {{ ref('deid_person_map') }}
where shift_days = 0 or abs(shift_days) > 180

-- 4. Intervals are preserved: each person's observation period has the same length.
union all
select 'observation period length changed', m.pseudo_person_id::text
from {{ ref('observation_period') }} as r
join {{ ref('deid_person_map') }} as m on m.person_id = r.person_id
join {{ ref('share_observation_period') }} as s on s.person_id = m.pseudo_person_id
where (s.observation_period_end_date - s.observation_period_start_date)
    <> (r.observation_period_end_date - r.observation_period_start_date)

-- 5. Nothing is lost: same row counts in both tiers.
union all
select 'row count differs: ' || t.name, null
from (
    select 'measurement' as name,
        (select count(*) from {{ ref('measurement') }}) as restricted,
        (select count(*) from {{ ref('share_measurement') }}) as shareable
    union all
    select 'condition_occurrence',
        (select count(*) from {{ ref('condition_occurrence') }}),
        (select count(*) from {{ ref('share_condition_occurrence') }})
    union all
    select 'drug_exposure',
        (select count(*) from {{ ref('drug_exposure') }}),
        (select count(*) from {{ ref('share_drug_exposure') }})
) as t
where t.restricted <> t.shareable

-- 6. Every shareable date is exactly its restricted date plus the person's shift. Rows are
--    matched through the keyed pseudonym of their id; a row with no match is a violation.
{% set shifted = [
    ('visit_occurrence', 'visit_occurrence_id', 'visit',
        ['visit_start_date', 'visit_end_date'], ['visit_start_datetime', 'visit_end_datetime']),
    ('condition_occurrence', 'condition_occurrence_id', 'condition',
        ['condition_start_date', 'condition_end_date'],
        ['condition_start_datetime', 'condition_end_datetime']),
    ('drug_exposure', 'drug_exposure_id', 'drug',
        ['drug_exposure_start_date', 'drug_exposure_end_date'],
        ['drug_exposure_start_datetime', 'drug_exposure_end_datetime']),
    ('measurement', 'measurement_id', 'measurement', ['measurement_date'], ['measurement_datetime']),
    ('observation', 'observation_id', 'observation', ['observation_date'], ['observation_datetime']),
    ('observation_period', 'observation_period_id', 'observation_period',
        ['observation_period_start_date', 'observation_period_end_date'], []),
] %}
{% for table, id_col, purpose, dates, stamps in shifted %}
union all
select 'date not shifted exactly: {{ table }}', r.{{ id_col }}::text
from {{ ref(table) }} as r
join {{ ref('deid_person_map') }} as m on m.person_id = r.person_id
left join {{ ref('share_' ~ table) }} as s on s.{{ id_col }} = {{ deid_id('r.' ~ id_col, purpose) }}
where s.{{ id_col }} is null
    or s.person_id <> m.pseudo_person_id
    {% for c in dates %}
    or s.{{ c }} is distinct from {{ shift_date('r.' ~ c) }}
    {% endfor %}
    {% for c in stamps %}
    or s.{{ c }} is distinct from {{ shift_ts('r.' ~ c) }}
    {% endfor %}
{% endfor %}
union all
select 'year of birth not from the shifted birth date', s.person_id::text
from {{ ref('person') }} as r
join {{ ref('deid_person_map') }} as m on m.person_id = r.person_id
join {{ ref('share_person') }} as s on s.person_id = m.pseudo_person_id
where s.year_of_birth > {{ ref_year - 90 }}
    and s.year_of_birth <> extract(year from {{ shift_ts('r.birth_datetime') }})::integer

-- 7. No column of any shareable table holds a restricted person_source_value.
{% if execute %}
{% for table in ['person', 'visit_occurrence', 'condition_occurrence', 'drug_exposure',
                 'measurement', 'observation', 'observation_period'] %}
{% for col in adapter.get_columns_in_relation(ref('share_' ~ table)) %}
union all
select 'person_source_value in {{ table }}.{{ col.name }}', s.{{ col.name }}::text
from {{ ref('share_' ~ table) }} as s
join (
    select distinct person_source_value as v from {{ ref('person') }}
    where person_source_value is not null
) as p on s.{{ col.name }}::text = p.v
{% endfor %}
{% endfor %}
{% endif %}

-- 8. The researcher role can only read the shareable tier: no write, TRUNCATE or CREATE
--    anywhere, and no read outside omop_shareable (ops, ops_secret, vocab, dbt, ...).
union all
select 'researcher role missing', 'dip_researcher'
where not exists (select 1 from pg_roles where rolname = 'dip_researcher')
union all
select 'researcher can ' || lower(priv) || ': ' || n.nspname || '.' || c.relname, null
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
cross join unnest(array['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'])
    as priv
where exists (select 1 from pg_roles where rolname = 'dip_researcher')
    and c.relkind in ('r', 'p', 'v', 'm', 'f')
    and n.nspname not in ('pg_catalog', 'information_schema')
    and n.nspname not like 'pg_toast%'
    and has_table_privilege('dip_researcher', c.oid, priv)
union all
select 'researcher can read: ' || n.nspname || '.' || c.relname, null
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
where exists (select 1 from pg_roles where rolname = 'dip_researcher')
    and c.relkind in ('r', 'p', 'v', 'm', 'f')
    and n.nspname not in ('pg_catalog', 'information_schema', 'omop_shareable')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp%'
    and has_table_privilege('dip_researcher', c.oid, 'SELECT')
union all
select 'researcher can use schema: ' || n.nspname, null
from pg_namespace as n
where exists (select 1 from pg_roles where rolname = 'dip_researcher')
    and n.nspname in ('ops', 'ops_secret', 'vocab', 'dbt', 'omop', 'deid', 'bronze', 'silver')
    and has_schema_privilege('dip_researcher', n.oid, 'USAGE')
union all
select 'researcher can create in schema: ' || n.nspname, null
from pg_namespace as n
where exists (select 1 from pg_roles where rolname = 'dip_researcher')
    and n.nspname not in ('pg_catalog', 'information_schema')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp%'
    and has_schema_privilege('dip_researcher', n.oid, 'CREATE')
union all
select 'researcher can create schemas in the database', current_database()
where exists (select 1 from pg_roles where rolname = 'dip_researcher')
    and has_database_privilege('dip_researcher', current_database(), 'CREATE')
