{{ config(schema='silver', materialized='view', tags=['healthcare.eligibility']) }}

-- Silver: every accepted eligibility row, typed. The intake already checked each value
-- against the contract (dip/csvfeed.py) and stored it trimmed, so the casts cannot fail.
select
    b.file_sha256,
    b.row_number,
    f.received_at,
    f.contract_version,
    b.record ->> 'member_id' as member_id,
    b.record ->> 'first_name' as first_name,
    b.record ->> 'last_name' as last_name,
    (b.record ->> 'dob')::date as dob,
    b.record ->> 'sex' as sex,
    b.record ->> 'plan_code' as plan_code,
    (b.record ->> 'coverage_start')::date as coverage_start,
    nullif(b.record ->> 'coverage_end', '')::date as coverage_end,
    nullif(b.record ->> 'patient_ref', '') as patient_ref
from {{ source('bronze', 'csv_record') }} as b
inner join {{ source('ops', 'intake_file') }} as f
    on f.feed = b.feed and f.file_sha256 = b.file_sha256
where b.feed = 'healthcare.eligibility'
