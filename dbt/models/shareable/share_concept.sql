{{ config(alias='concept') }}

-- Researchers need the vocabulary to read concept ids. With the Eunomia shard (CI) it is
-- public reference data; with a local Athena load it is the downloader's licensed copy,
-- readable only inside this warehouse and never exported (dip/extract.py excludes it).
select * from {{ ref('concept') }}
