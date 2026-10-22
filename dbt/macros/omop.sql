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
        when 'http://hl7.org/fhir/sid/icd-10-cm' then 'ICD10CM'
        when 'http://hl7.org/fhir/sid/icd-10' then 'ICD10'
        -- MIMIC-IV on FHIR publishes standard codes under MIMIC-local system URIs.
        when 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-diagnosis-icd9' then 'ICD9CM'
        when 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-diagnosis-icd10' then 'ICD10CM'
        when 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-medication-ndc' then 'NDC'
        -- MIMIC lab itemids: mapped to LOINC by vocab.source_to_concept_map when loaded.
        when 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-d-labitems' then 'MIMIC_LAB'
        else 'LOCAL'
    end
{%- endmacro %}

{# ICD-9-CM / ICD-10-CM codes as OMOP stores them (with the dot). MIMIC stores them
   without one: "4019" -> "401.9", "E8889" -> "E888.9", "I10" stays "I10". #}
{% macro icd_with_dot(code, vocabulary_id) -%}
    case
        when {{ code }} is null or position('.' in {{ code }}) > 0 then {{ code }}
        when {{ vocabulary_id }} = 'ICD9CM' and left({{ code }}, 1) = 'E' and length({{ code }}) > 4
            then left({{ code }}, 4) || '.' || substr({{ code }}, 5)
        when {{ vocabulary_id }} in ('ICD9CM', 'ICD10CM', 'ICD10') and length({{ code }}) > 3
            and left({{ code }}, 1) <> 'E'
            then left({{ code }}, 3) || '.' || substr({{ code }}, 4)
        when {{ vocabulary_id }} in ('ICD10CM', 'ICD10') and length({{ code }}) > 3
            then left({{ code }}, 3) || '.' || substr({{ code }}, 4)
        else {{ code }}
    end
{%- endmacro %}

{# OMOP type concept for every record here: 32817 "EHR". #}
{% macro ehr_type_concept() -%}32817{%- endmacro %}

{# Does a staged Observation row go to `measurement`? Its standard concept is in the
   Measurement domain, or it is unmapped and categorized as a lab or vital sign. Never NULL
   (a missing domain or category counts as false), so `measurement` takes the rows where it
   is true and `observation` exactly the rest: no row lands in both or in neither. #}
{% macro routes_to_measurement(std, obs) -%}
    (coalesce({{ std }}.standard_domain_id = 'Measurement', false)
        or ({{ std }}.standard_concept_id is null
            and coalesce({{ obs }}.category in ('laboratory', 'vital-signs'), false)))
{%- endmacro %}
