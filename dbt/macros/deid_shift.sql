{# Shift a date or timestamp by the person's offset (column shift_days from deid_person_map). #}
{% macro shift_date(expr, map_alias='m') -%}
    ({{ expr }} + {{ map_alias }}.shift_days)
{%- endmacro %}

{% macro shift_ts(expr, map_alias='m') -%}
    ({{ expr }} + {{ map_alias }}.shift_days * interval '1 day')
{%- endmacro %}
