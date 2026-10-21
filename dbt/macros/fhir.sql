{# Helpers for reading FHIR JSON stored in bronze.fhir_resource.resource (jsonb). #}

{# FHIR date or dateTime text -> timestamp in UTC. Date-only values are midnight UTC, so
   results never depend on the session time zone. #}
{% macro fhir_ts(expr) -%}
    case
        when ({{ expr }}) is null then null
        when length({{ expr }}) = 10 then ({{ expr }})::date::timestamp
        else (({{ expr }})::timestamptz at time zone 'UTC')
    end
{%- endmacro %}

{# 'urn:uuid:abc' or 'Encounter/abc' -> 'abc' #}
{% macro fhir_ref_id(expr) -%}
    regexp_replace({{ expr }}, '^(urn:uuid:|[A-Za-z]+/)', '')
{%- endmacro %}

{# First coding in a CodeableConcept with the given system: its code, or its display. #}
{% macro fhir_code(concept, system, field='code') -%}
    (
        select c ->> '{{ field }}'
        from jsonb_array_elements(coalesce({{ concept }} -> 'coding', '[]'::jsonb)) as c
        where c ->> 'system' = '{{ system }}'
        limit 1
    )
{%- endmacro %}

{# The coding to use from a CodeableConcept: the first one whose system is in `systems`
   (in that order of preference), else the first coding. Returns the coding as jsonb. #}
{% macro fhir_pick_coding(concept, systems) -%}
    (
        select c
        from jsonb_array_elements(coalesce({{ concept }} -> 'coding', '[]'::jsonb))
            with ordinality as t (c, n)
        order by
            case c ->> 'system'
                {%- for s in systems %} when '{{ s }}' then {{ loop.index }}{% endfor %}
                else 999
            end,
            n
        limit 1
    )
{%- endmacro %}

{# US Core race/ethnicity extension -> OMB category code. #}
{% macro us_core_omb(resource, kind) -%}
    (
        select inner_ext -> 'valueCoding' ->> 'code'
        from jsonb_array_elements(coalesce({{ resource }} -> 'extension', '[]'::jsonb)) as ext,
            jsonb_array_elements(coalesce(ext -> 'extension', '[]'::jsonb)) as inner_ext
        where ext ->> 'url' like '%us-core-{{ kind }}'
          and inner_ext ->> 'url' = 'ombCategory'
        limit 1
    )
{%- endmacro %}

{# A local timestamp without an offset (MIMIC relational tables, Boston time) -> UTC, so
   both MIMIC paths agree with each other (the FHIR export carries the offsets). #}
{% macro local_to_utc(expr, tz='America/New_York') -%}
    ((({{ expr }})::timestamp at time zone '{{ tz }}') at time zone 'UTC')
{%- endmacro %}
