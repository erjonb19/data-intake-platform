{#
  OMOP data quality checks as dbt generic tests, each named after the OHDSI Data Quality
  Dashboard (DQD) check it mirrors. We do not run DQD itself (an R package); these are
  equivalents written in SQL. Each test returns the failing rows, or for threshold checks
  a single row when the failure rate is above `max_ratio`.

  Category, DQD check name and threshold go in the test's `meta`, and the on-run-end hook
  (macros/dq_results.sql) records every result in ops.dq_results.
#}

{# fkDomain: a concept column only holds concepts of the expected domain (or 0). #}
{% test dqd_fk_domain(model, column_name, domain) %}
select m.{{ column_name }} as concept_id, c.domain_id
from {{ model }} as m
join {{ ref('concept') }} as c on c.concept_id = m.{{ column_name }}
where m.{{ column_name }} <> 0 and c.domain_id <> '{{ domain }}'
{% endtest %}

{# isForeignKey (to CONCEPT): every non-null concept id exists in the vocabulary. #}
{% test dqd_is_foreign_key_concept(model, column_name) %}
select m.{{ column_name }} as concept_id
from {{ model }} as m
left join {{ ref('concept') }} as c on c.concept_id = m.{{ column_name }}
where m.{{ column_name }} is not null and c.concept_id is null
{% endtest %}

{# isStandardValidConcept: non-zero concept ids are standard ('S') and not invalidated. #}
{% test dqd_is_standard_valid_concept(model, column_name) %}
select m.{{ column_name }} as concept_id
from {{ model }} as m
join {{ ref('concept') }} as c on c.concept_id = m.{{ column_name }}
where m.{{ column_name }} <> 0
  and (c.standard_concept is distinct from 'S' or c.invalid_reason is not null)
{% endtest %}

{# standardConceptRecordCompleteness: share of rows with concept_id 0 is at most max_ratio. #}
{% test dqd_standard_concept_record_completeness(model, column_name, max_ratio) %}
select failing, total, failing::numeric / nullif(total, 0) as ratio
from (
    select count(*) filter (where {{ column_name }} = 0) as failing, count(*) as total
    from {{ model }}
) as s
where failing::numeric / nullif(total, 0) > {{ max_ratio }}
{% endtest %}

{# sourceValueCompleteness: rows keep their source value, so unmapped codes stay usable. #}
{% test dqd_source_value_completeness(model, column_name, concept_column) %}
select *
from {{ model }}
where {{ concept_column }} = 0 and ({{ column_name }} is null or {{ column_name }} = '')
{% endtest %}

{# plausibleAfterBirth: the event date is not before the person's birth. #}
{% test dqd_plausible_after_birth(model, column_name) %}
select m.person_id, m.{{ column_name }}, p.birth_datetime
from {{ model }} as m
join {{ ref('person') }} as p on p.person_id = m.person_id
where m.{{ column_name }} < p.birth_datetime::date
{% endtest %}

{# plausibleStartBeforeEnd: the start date is on or before the end date. #}
{% test dqd_plausible_start_before_end(model, column_name, end_column) %}
select *
from {{ model }}
where {{ end_column }} is not null and {{ column_name }} > {{ end_column }}
{% endtest %}

{# plausibleValueLow / plausibleValueHigh, for one measurement concept. #}
{% test dqd_plausible_value_range(model, column_name, concept_id, low, high) %}
select measurement_id, {{ column_name }}
from {{ model }}
where measurement_concept_id = {{ concept_id }}
  and {{ column_name }} is not null
  and ({{ column_name }} < {{ low }} or {{ column_name }} > {{ high }})
{% endtest %}

{# plausibleUnitConceptIds, for one measurement concept. #}
{% test dqd_plausible_unit_concept_ids(model, column_name, concept_id, allowed) %}
select measurement_id, {{ column_name }}
from {{ model }}
where measurement_concept_id = {{ concept_id }}
  and value_as_number is not null
  and {{ column_name }} not in ({{ allowed | join(', ') }})
{% endtest %}

{# withinVisitDates: the event falls inside its visit (with a one-week tolerance, as DQD). #}
{% test dqd_within_visit_dates(model, column_name) %}
select m.visit_occurrence_id, m.{{ column_name }}, v.visit_start_date, v.visit_end_date
from {{ model }} as m
join {{ ref('visit_occurrence') }} as v on v.visit_occurrence_id = m.visit_occurrence_id
where m.{{ column_name }} < v.visit_start_date - 7 or m.{{ column_name }} > v.visit_end_date + 7
{% endtest %}

{#
  inObservationPeriod: every clinical event falls inside one of the person's observation
  periods. This is an OMOP CDM convention (THEMIS) rather than a named DQD check; it is
  what makes longitudinal questions about "the last year" trustworthy.
#}
{% test omop_in_observation_period(model, column_name) %}
select m.person_id, m.{{ column_name }}
from {{ model }} as m
where not exists (
    select 1
    from {{ ref('observation_period') }} as op
    where op.person_id = m.person_id
      and m.{{ column_name }} between op.observation_period_start_date
          and op.observation_period_end_date
)
{% endtest %}

{# measurePersonCompleteness: share of persons with no rows in this table <= max_ratio. #}
{% test dqd_measure_person_completeness(model, column_name, max_ratio) %}
select missing, total, missing::numeric / nullif(total, 0) as ratio
from (
    select
        count(*) filter (where not exists (
            select 1 from {{ model }} as m where m.person_id = p.person_id
        )) as missing,
        count(*) as total
    from {{ ref('person') }} as p
) as s
where missing::numeric / nullif(total, 0) > {{ max_ratio }}
{% endtest %}

{# plausibleValueLow/High for year_of_birth: after 1850 and not in the future. #}
{% test dqd_plausible_year_of_birth(model, column_name) %}
select person_id, {{ column_name }}
from {{ model }}
where {{ column_name }} < 1850 or {{ column_name }} > extract(year from current_date)
{% endtest %}
