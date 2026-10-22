{{ config(schema='gold', materialized='table', tags=['healthcare.eligibility']) }}

-- Gold: each member's current eligibility, from the most recently received file that
-- lists them (the feed sends full daily files). Ties on received_at, which only happen
-- when several files are processed in one intake run, go to the higher sha256 so the
-- result is deterministic.
select distinct on (member_id)
    member_id,
    first_name,
    last_name,
    dob,
    sex,
    plan_code,
    coverage_start,
    coverage_end,
    patient_ref,
    received_at as as_of,
    file_sha256 as source_file_sha256
from {{ ref('eligibility_record') }}
order by member_id, received_at desc, file_sha256 desc
