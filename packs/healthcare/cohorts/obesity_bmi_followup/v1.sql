-- title: Adults with a BMI of 30 or more, re-measured a year or more later
-- question: How many adults had a body mass index of 30 or more, a BMI measured again 365
-- question: or more days later, and a later BMI at least 5% lower than the first one of 30+?
-- caveat: Intervals between measurements are exact; the age is approximate (year of the
-- caveat: measurement minus year of birth).
-- Concepts: 3038553 body mass index (LOINC 39156-5).
with bmi as (
    select m.person_id, m.measurement_date, m.value_as_number
    from omop_shareable.measurement as m
    where m.measurement_concept_id = 3038553 and m.value_as_number is not null
),

first_obese as (
    select distinct on (b.person_id) b.person_id, b.measurement_date, b.value_as_number
    from bmi as b
    join omop_shareable.person as p on p.person_id = b.person_id
    where b.value_as_number >= 30
        and extract(year from b.measurement_date) - p.year_of_birth >= 18
    order by b.person_id, b.measurement_date
),

remeasured as (
    select f.person_id, f.value_as_number as first_value, b.value_as_number as later_value
    from first_obese as f
    join bmi as b on b.person_id = f.person_id and b.measurement_date >= f.measurement_date + 365
)

select 0 as step, 'persons in the tier' as criterion, count(*) as persons
from omop_shareable.person
union all
select 1, 'adults with a BMI of 30+', count(*) from first_obese
union all
select 2, 'a BMI measured again 365+ days later', count(distinct person_id) from remeasured
union all
select 3, 'a later BMI at least 5% lower', count(distinct person_id)
from remeasured where later_value <= first_value * 0.95
order by step
