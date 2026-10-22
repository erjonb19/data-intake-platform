{{ config(schema='gold', materialized='table', tags=['file_feed', 'healthcare.eligibility']) }}

-- Gold: one row per member ever listed, from the newest file that lists them. Files are
-- ordered by business date (file_date), then by when they were received, so a late copy
-- of an older file never overrides newer data.
--
-- The feed sends full files, so a member is current only if the newest accepted file
-- lists them: in_latest_file. A member missing from it (disenrolled), or whose row in it
-- was quarantined, keeps their last known row here with in_latest_file = false; the
-- quarantine says why. A file that was quarantined whole is not "accepted" and is skipped.
with files as (
    select distinct file_sha256, file_date, received_at
    from {{ ref('eligibility_record') }}
),

latest_file as (
    select file_sha256
    from files
    order by file_date desc, received_at desc, file_sha256 desc
    limit 1
)

select distinct on (r.member_id)
    r.member_id,
    r.first_name,
    r.last_name,
    r.dob,
    r.sex,
    r.plan_code,
    r.coverage_start,
    r.coverage_end,
    r.patient_ref,
    r.file_date,
    r.received_at,
    r.file_sha256 as source_file_sha256,
    r.file_sha256 = (select file_sha256 from latest_file) as in_latest_file
from {{ ref('eligibility_record') }} as r
order by r.member_id, r.file_date desc, r.received_at desc, r.file_sha256 desc
