{{ config(alias='cdm_source') }}

-- OMOP CDM_SOURCE: what this shareable tier holds. Consumers outside this warehouse (the
-- clinical-drift-monitor's cohort service, R7) read it first and refuse anything but
-- DIP-SYNTHEA. The classification fails closed:
--   DIP-MIMIC    any MIMIC source (an intake feed or relational table named mimic.*, or a
--                build of the MIMIC relational tables), whatever else is loaded with it
--   DIP-SYNTHEA  otherwise, if the Synthea FHIR feed is loaded and every other source is
--                one of this repo's synthetic healthcare.* feeds
--   DIP-UNKNOWN  anything else, including an empty warehouse
-- The classification is macro cdm_source_kind_sql. The on-run-end macro
-- grant_cohort_reader() works it out again, live, rather than trusting this table, which
-- a partial dbt run may not have rebuilt.
{% set load_info = adapter.get_relation(database=target.database, schema='vocab',
                                         identifier='load_info') %}
with kind as (
    {{ cdm_source_kind_sql(source('ops', 'intake_file')) }}
)

select
    case k.abbreviation
        when 'DIP-SYNTHEA' then 'Data intake platform: Synthea synthetic patients'
        when 'DIP-MIMIC' then 'Data intake platform: MIMIC-IV demo (real, de-identified)'
        else 'Data intake platform: unclassified sources'
    end::varchar(255) as cdm_source_name,
    k.abbreviation::varchar(25) as cdm_source_abbreviation,
    'data-intake-platform (local build)'::varchar(255) as cdm_holder,
    case k.abbreviation
        when 'DIP-SYNTHEA' then 'Synthetic patients (Synthea, FHIR R4), de-identified tier: '
            || 'pseudonymous ids, dates shifted per person by 1-180 days, year of birth only.'
        when 'DIP-MIMIC' then 'REAL de-identified patients from the MIMIC-IV demo '
            || '(PhysioNet, ODbL). Local use only; never given to outside services.'
        else 'Sources could not be classified; treat as restricted.'
    end::text as source_description,
    'https://github.com/erjonb19/data-intake-platform/blob/main/docs/research/README.md'::varchar(255)
        as source_documentation_reference,
    'https://github.com/erjonb19/data-intake-platform/tree/main/dbt'::varchar(255)
        as cdm_etl_reference,
    (
        select max(received_at)::date from {{ source('ops', 'intake_file') }}
        where status <> 'quarantined'
    ) as source_release_date,
    current_date as cdm_release_date,
    'v5.4'::varchar(10) as cdm_version,
    756265 as cdm_version_concept_id,
    {% if load_info %}
    coalesce(
        -- CDM allows 20 characters: the Athena release, or the Eunomia shard's commit.
        (select coalesce(provenance ->> 'release',
                         'Eunomia ' || left(provenance ->> 'commit', 12))
         from {{ load_info }} limit 1),
        'unknown'
    )::varchar(20)
    {% else %}
    'unknown'::varchar(20)
    {% endif %} as vocabulary_version
from kind as k
