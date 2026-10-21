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
