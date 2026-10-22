-- title: Adults with hypertension followed up by blood pressure in the year after diagnosis
-- question: How many adults with essential hypertension were observed for at least a year
-- question: after their first diagnosis, had two or more systolic readings in that year, and
-- question: had a systolic reading of 140 mmHg or more in it?
-- caveat: The year after diagnosis is exact (intervals within a person are preserved); the
-- caveat: diagnosis date itself is shifted by up to 180 days. Age is year of diagnosis minus
-- caveat: year of birth.
-- Concepts: 320128 essential hypertension; 3004249 systolic blood pressure (LOINC 8480-6).
with htn as (
    select c.person_id, min(c.condition_start_date) as first_dx
    from omop_shareable.condition_occurrence as c
    where c.condition_concept_id = 320128
    group by c.person_id
),

adult_htn as (
    select h.person_id, h.first_dx
    from htn as h
    join omop_shareable.person as p on p.person_id = h.person_id
    where extract(year from h.first_dx) - p.year_of_birth >= 18
),

observed_a_year as (
    select a.person_id, a.first_dx
    from adult_htn as a
    join omop_shareable.observation_period as op on op.person_id = a.person_id
    where op.observation_period_end_date >= a.first_dx + 365
),

first_year_sbp as (
    select o.person_id, m.value_as_number
    from observed_a_year as o
    join omop_shareable.measurement as m on m.person_id = o.person_id
    where m.measurement_concept_id = 3004249
        and m.measurement_date > o.first_dx
        and m.measurement_date <= o.first_dx + 365
),

followed_up as (
    select person_id from first_year_sbp group by person_id having count(*) >= 2
),

uncontrolled as (
    select distinct f.person_id
    from followed_up as f
    join first_year_sbp as s on s.person_id = f.person_id
    where s.value_as_number >= 140
)

select 0 as step, 'persons in the tier' as criterion, count(*) as persons
from omop_shareable.person
union all
select 1, 'adults (18+) with essential hypertension', count(*) from adult_htn
union all
select 2, 'observed for 365+ days after the first diagnosis', count(distinct person_id)
from observed_a_year
union all
select 3, '2+ systolic readings in the year after diagnosis', count(*) from followed_up
union all
select 4, 'a systolic reading of 140+ mmHg in that year', count(*) from uncontrolled
order by step
