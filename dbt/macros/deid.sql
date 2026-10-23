{#
  De-identification helpers (R4). The key is read from ops_secret.deid_key inside SQL, so
  it never appears in compiled SQL or logs. pgcrypto provides hmac().
#}

{% macro deid_key() -%}
    (select key from ops_secret.deid_key where key_id = 'default')
{%- endmacro %}

{# Keyed pseudonym: HMAC-SHA256(key, purpose:value) -> 60-bit integer. The purpose prefix
   keeps pseudonyms for different tables unrelated to each other. #}
{% macro deid_id(expr, purpose) -%}
    ('x' || substr(encode(hmac('{{ purpose }}:' || ({{ expr }})::text, {{ deid_key() }}, 'sha256'), 'hex'), 1, 15))::bit(60)::bigint
{%- endmacro %}

{# Per-person date shift in days: 1 to 180, either direction, never 0. Stable for a key. #}
{% macro deid_shift_days(person_key) -%}
    (
        with h as (
            select ('x' || substr(encode(hmac('shift:' || ({{ person_key }})::text, {{ deid_key() }}, 'sha256'), 'hex'), 1, 8))::bit(32)::bigint as v
        )
        select ((v % 180) + 1)::integer * (case when (v / 180) % 2 = 0 then 1 else -1 end)
        from h
    )
{%- endmacro %}

{# Fails the run early, with a clear message, if no key is installed. #}
{% macro assert_deid_key_installed() %}
    {% if execute %}
        {% set found = run_query("select count(*) from ops_secret.deid_key where key_id = 'default'").columns[0].values()[0] %}
        {% if found == 0 %}
            {{ exceptions.raise_compiler_error("No de-identification key installed. Set DIP_DEID_KEY and run `python -m dip deid-key-install` (the DAG does this).") }}
        {% endif %}
    {% endif %}
{% endmacro %}

{# on-run-end: researchers may use the shareable schema (table grants come from dbt). #}
{% macro grant_shareable_usage() %}
    {% if execute %}
        {% set ready = run_query("select (select count(*) from pg_roles where rolname = 'dip_researcher') * (select count(*) from pg_namespace where nspname = 'omop_shareable')").columns[0].values()[0] %}
        {% if ready %}
            grant usage on schema omop_shareable to dip_researcher;
        {% else %}
            {{ log("role dip_researcher or schema omop_shareable missing; no usage grant", info=True) }}
            select 1;
        {% endif %}
    {% endif %}
{% endmacro %}

{#
  R7: what the warehouse holds, as one row with one column, `abbreviation`:
    DIP-MIMIC    any MIMIC source (an intake feed or relational table named mimic.*, or a
                 build of the MIMIC relational tables), whatever else is loaded with it
    DIP-SYNTHEA  otherwise, if the Synthea FHIR feed is loaded and every other source is
                 one of this repo's generated packs' feeds (healthcare.*, finance.*: all
                 synthetic or invented; finance never feeds OMOP)
    DIP-UNKNOWN  anything else, including an empty warehouse
  Feed names are the signal. The guard against MIMIC data sent through the Synthea feed is
  that feed's contract: MIMIC's dates are shifted past 2100, which its FUTURE_DATE check
  rejects (docs/research/cohort-service.md).
  MIMIC counts whatever its status (a file or table later quarantined or reloaded may
  already have fed OMOP), and so does a block row: once any dbt run has seen MIMIC, the
  hook writes ops.cohort_reader_block, and the warehouse stays DIP-MIMIC until a person
  deletes that row (after rebuilding everything from Synthea alone).
  Shared by the cdm_source model and grant_cohort_reader, so both classify alike.
#}
{% macro cdm_source_kind_sql(intake_file) -%}
    {%- set relational = adapter.get_relation(database=target.database, schema='ops',
                                               identifier='relational_load') -%}
    {%- set block = adapter.get_relation(database=target.database, schema='ops',
                                          identifier='cohort_reader_block') -%}
    with sources as (
        select feed as source_name from {{ intake_file }}
        where status <> 'quarantined' or feed like 'mimic.%'
        {% if relational -%}
        union all
        select source || '.' || table_name from {{ relational }}
        where status = 'loaded' or source like 'mimic%'
        {% endif -%}
        {% if block -%}
        union all
        select 'mimic.blocked' from {{ block }}
        {% endif -%}
        {% if var('source_kind', 'fhir') == 'mimic_relational' -%}
        union all
        select 'mimic.relational_build'
        {% endif %}
    )
    select
        case
            when exists (select 1 from sources where source_name like 'mimic.%')
                then 'DIP-MIMIC'
            when exists (
                select 1 from sources where source_name = 'healthcare.fhir_patient_bundle'
            ) and not exists (
                select 1 from sources
                where source_name not like 'healthcare.%' and source_name not like 'finance.%'
            )
                then 'DIP-SYNTHEA'
            else 'DIP-UNKNOWN'
        end as abbreviation
{%- endmacro %}

{#
  on-run-end (R7): the cohort reader (dip_cohort_reader, a login role for the
  clinical-drift-monitor's cohort service) may read the shareable tier only while the
  warehouse holds Synthea alone. The hook classifies the warehouse live (it does not trust
  omop_shareable.cdm_source, which a partial run may not have rebuilt) and then:
    - anything but DIP-SYNTHEA: every grant the reader has on the tier is revoked;
    - DIP-MIMIC also writes ops.cohort_reader_block, which keeps it DIP-MIMIC from then on;
    - DIP-SYNTHEA: SELECT on exactly the shareable tables this run rebuilt successfully
      (never `concept`: with a local Athena load it is the downloader's licensed copy),
      plus USAGE on the schema. Tables this run did not rebuild keep what they had, which
      they were only ever given in a Synthea-only run that rebuilt them.
  A rebuilt table starts with no grants, so a run that fails before this hook leaves it
  unreadable. Without run results (dbt run-operation grant_cohort_reader) it only revokes.
#}
{% macro grant_cohort_reader(results=none) %}
    {% if execute %}
        {% set ready = run_query("select count(*) from pg_roles where rolname = 'dip_cohort_reader'").columns[0].values()[0] %}
        {% set intake_file = adapter.get_relation(database=target.database, schema='ops',
                                                   identifier='intake_file') %}
        {% set live = 'DIP-UNKNOWN' %}
        {% if intake_file %}
            {% set live = run_query(cdm_source_kind_sql(intake_file)).columns[0].values()[0] %}
        {% endif %}
        {% set rebuilt = [] %}
        {% for r in (results or []) %}
            {% if r.node.resource_type == 'model' and 'shareable' in r.node.fqn
                  and r.status == 'success' and r.node.alias != 'concept'
                  and r.node.config.materialized == 'table' %}
                {% do rebuilt.append(r.node.alias) %}
            {% endif %}
        {% endfor %}
        {% if not ready %}
            {{ log("role dip_cohort_reader missing; no cohort reader grants", info=True) }}
        {% elif live != 'DIP-SYNTHEA' %}
            {{ log("warehouse holds " ~ live ~ ": dip_cohort_reader gets nothing; revoking its grants", info=True) }}
            {% if live == 'DIP-MIMIC' %}
                {% do run_query("""
                    create table if not exists ops.cohort_reader_block (
                        blocked_at timestamptz not null default now(),
                        reason text not null
                    );
                    insert into ops.cohort_reader_block (reason)
                    select 'a dbt run saw a MIMIC source'
                    where not exists (select 1 from ops.cohort_reader_block);
                    commit;
                """) %}
            {% endif %}
            {% do run_query("""
                do $$
                begin
                    if exists (select 1 from pg_namespace where nspname = 'omop_shareable') then
                        revoke all on all tables in schema omop_shareable from dip_cohort_reader;
                        revoke usage on schema omop_shareable from dip_cohort_reader;
                    end if;
                end
                $$;
                commit;
            """) %}
        {% elif rebuilt %}
            {{ log("warehouse holds DIP-SYNTHEA: dip_cohort_reader may read " ~ (rebuilt | sort | join(', ')), info=True) }}
            {% set sql %}
                grant usage on schema omop_shareable to dip_cohort_reader;
                {% for t in rebuilt | sort %}
                grant select on {{ adapter.quote('omop_shareable') }}.{{ adapter.quote(t) }} to dip_cohort_reader;
                {% endfor %}
                commit;
            {% endset %}
            {% do run_query(sql) %}
        {% endif %}
    {% endif %}
    select 1;
{% endmacro %}
