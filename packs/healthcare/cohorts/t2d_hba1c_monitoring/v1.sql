-- title: Adults with type 2 diabetes diagnosed before 2020 and monitored by HbA1c
-- question: How many adults with type 2 diabetes first diagnosed before 2020 had two or
-- question: more HbA1c results in the last 365 days of their observation, and how many of
-- question: them had at least one result of 7.0% or more in that year?
-- caveat: Dates are shifted per person by up to 180 days, so "before 2020" is approximate
-- caveat: for diagnoses near the cut-off; the 365-day window is exact (intervals within a
-- caveat: person are preserved). Age is the year of diagnosis minus the year of birth.
-- Concepts (OMOP standard): 201826 type 2 diabetes mellitus; 3004410 HbA1c (LOINC 4548-4),
-- unit % (8554).
with persons as (
    select person_id, year_of_birth from omop_shareable.person
),

t2d as (
    select c.person_id, min(c.condition_start_date) as first_dx
    from omop_shareable.condition_occurrence as c
    where c.condition_concept_id = 201826
    group by c.person_id
),

adult_t2d as (
    select t.person_id, t.first_dx
    from t2d as t
    join persons as p on p.person_id = t.person_id
    where extract(year from t.first_dx) - p.year_of_birth >= 18
),

before_2020 as (
    select person_id, first_dx from adult_t2d where first_dx < date '2020-01-01'
),

last_year as (
    select m.person_id, m.value_as_number
    from omop_shareable.measurement as m
    join omop_shareable.observation_period as op on op.person_id = m.person_id
    where m.measurement_concept_id = 3004410
        and m.measurement_date > op.observation_period_end_date - 365
        and m.measurement_date <= op.observation_period_end_date
),

monitored as (
    select b.person_id
    from before_2020 as b
    join last_year as y on y.person_id = b.person_id
    group by b.person_id
    having count(*) >= 2
),

above_target as (
    select distinct m.person_id
    from monitored as m
    join last_year as y on y.person_id = m.person_id
    where y.value_as_number >= 7.0
)

select 0 as step, 'persons in the tier' as criterion, count(*) as persons from persons
union all
select 1, 'adults (18+) with type 2 diabetes', count(*) from adult_t2d
union all
select 2, 'first diagnosed before 2020-01-01', count(*) from before_2020
union all
select 3, '2+ HbA1c results in the last 365 days of observation', count(*) from monitored
union all
select 4, 'an HbA1c of 7.0% or more in that year', count(*) from above_target
order by step
