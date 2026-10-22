-- title: Type 2 diabetes progressing to diabetic kidney disease
-- question: Of adults with type 2 diabetes, how many were later diagnosed with a diabetic
-- question: kidney disorder (at least 30 days after their first type 2 diabetes diagnosis),
-- question: and how many of those within five years of it?
-- caveat: Time from diabetes to kidney disease is exact within a person; calendar dates are
-- caveat: shifted. Kidney concepts are listed explicitly (no concept-ancestor table in this
-- caveat: tier), so a kidney diagnosis coded outside the list is not counted.
-- Concepts: 201826 type 2 diabetes mellitus; kidney: 192279 disorder of kidney due to
-- diabetes mellitus, 45769905 microalbuminuria due to type 2 diabetes, 443614 chronic kidney
-- disease stage 1, 443601/443597/443612/443611 stages 2-5, 193782 end-stage renal disease.
with t2d as (
    select c.person_id, min(c.condition_start_date) as first_dx
    from omop_shareable.condition_occurrence as c
    where c.condition_concept_id = 201826
    group by c.person_id
),

adult_t2d as (
    select t.person_id, t.first_dx
    from t2d as t
    join omop_shareable.person as p on p.person_id = t.person_id
    where extract(year from t.first_dx) - p.year_of_birth >= 18
),

kidney_after as (
    select a.person_id, a.first_dx, min(k.condition_start_date) as first_kidney
    from adult_t2d as a
    join omop_shareable.condition_occurrence as k on k.person_id = a.person_id
    where k.condition_concept_id in (192279, 45769905, 443614, 443601, 443597, 443612,
                                     443611, 193782)
        and k.condition_start_date >= a.first_dx + 30
    group by a.person_id, a.first_dx
)

select 0 as step, 'persons in the tier' as criterion, count(*) as persons
from omop_shareable.person
union all
select 1, 'adults (18+) with type 2 diabetes', count(*) from adult_t2d
union all
select 2, 'a diabetic kidney diagnosis 30+ days after the first diabetes diagnosis', count(*)
from kidney_after
union all
select 3, 'within 5 years (1,826 days) of it', count(*)
from kidney_after where first_kidney <= first_dx + 1826
order by step
