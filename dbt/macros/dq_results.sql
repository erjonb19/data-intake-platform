{#
  on-run-end hook: record every test result of the run in ops.dq_results, so DQ history is
  queryable (and, from Stage 3, exposed read-only through MCP). Tests carry their DQ
  category and DQD check name in `meta`.
#}
{% macro store_dq_results(results) %}
  {% if execute %}
    {% set rows = [] %}
    {% for r in results if r.node.resource_type == 'test' %}
      {% set meta = r.node.config.meta or {} %}
      {% set model = (r.node.attached_node or '').split('.')[-1] %}
      {% do rows.append(
          "('" ~ invocation_id ~ "', '" ~ r.node.name ~ "', '" ~ model ~ "', '"
          ~ (r.node.column_name or '') ~ "', '" ~ (meta.get('dq_category') or 'integrity')
          ~ "', '" ~ (meta.get('dqd_check') or '') ~ "', '" ~ r.status ~ "', "
          ~ (r.failures if r.failures is not none else 'null') ~ ", now())"
      ) %}
    {% endfor %}
    create table if not exists ops.dq_results (
        invocation_id text not null,
        test_name text not null,
        model text,
        column_name text,
        dq_category text not null,
        dqd_check text,
        status text not null,
        failures bigint,
        recorded_at timestamptz not null,
        primary key (invocation_id, test_name)
    );
    {% if rows %}
    insert into ops.dq_results values {{ rows | join(',\n') }};
    {% endif %}
  {% endif %}
{% endmacro %}
