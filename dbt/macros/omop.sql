{# Stable 60-bit integer surrogate key from a text natural key. OMOP needs integer keys;
   hashing (not row_number) keeps IDs stable across loads, so a person keeps their
   person_id as new data arrives. Uniqueness is enforced by dbt tests. #}
{% macro omop_id(expr) -%}
    ('x' || substr(md5({{ expr }}), 1, 15))::bit(60)::bigint
{%- endmacro %}

{# FHIR code system URI -> OMOP vocabulary_id #}
{% macro fhir_system_to_vocabulary(system) -%}
    case {{ system }}
        when 'http://snomed.info/sct' then 'SNOMED'
        when 'http://loinc.org' then 'LOINC'
        when 'http://www.nlm.nih.gov/research/umls/rxnorm' then 'RxNorm'
        when 'http://unitsofmeasure.org' then 'UCUM'
    end
{%- endmacro %}

{# OMOP type concept for every record here: 32817 "EHR". #}
{% macro ehr_type_concept() -%}32817{%- endmacro %}
