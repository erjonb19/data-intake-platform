{{ config(schema='silver', materialized='view', tags=['file_feed', 'healthcare.eligibility']) }}

-- Silver: every accepted eligibility row, typed. The intake already checked each value
-- against the contract (dip/csvfeed.py) and stored it trimmed, so the casts cannot fail.
-- file_date is the business date in the file's name (elig_YYYYMMDD.csv), validated at
-- intake (FILE.NAME_DATE); a name with no date falls back to the UTC date received.
-- Identical content can be delivered again under a newer name: its rows then count as of
-- their latest delivery, so a resent file is still the latest file.
with deliveries as (
    select distinct on (d.file_sha256)
        d.file_sha256,
        d.source_key,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) as file_date,
        d.received_at
    from {{ source('ops', 'intake_delivery') }} as d
    where d.feed = 'healthcare.eligibility'
    order by
        d.file_sha256,
        coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) desc,
        d.received_at desc,
        d.source_key desc
)

select
    b.file_sha256,
    b.row_number,
    d.source_key,
    d.file_date,
    d.received_at,
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
inner join deliveries as d on d.file_sha256 = b.file_sha256
where b.feed = 'healthcare.eligibility'
