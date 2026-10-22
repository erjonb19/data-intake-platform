{{ config(schema='silver', materialized='view', tags=['file_feed', 'healthcare.eligibility']) }}

-- Silver: every accepted eligibility row, typed. The intake already checked each value
-- against the contract (dip/csvfeed.py) and stored it trimmed, so the casts cannot fail.
-- file_date is the file's business date, from its name (elig_YYYYMMDD.csv); a file named
-- otherwise falls back to the UTC date it was received.
select
    b.file_sha256,
    b.row_number,
    f.source_key,
    coalesce(
        to_date(substring(f.source_key from 'elig_([0-9]{8})[^/]*$'), 'YYYYMMDD'),
        (f.received_at at time zone 'UTC')::date
    ) as file_date,
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
