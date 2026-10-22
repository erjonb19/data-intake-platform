-- Staging contract between sources and OMOP: every source (FHIR today, MIMIC relational
-- tables with source_kind=mimic_relational) produces these columns. See _staging.yml.
select patient_id, source_identifier, gender, birth_date, deceased_at, race_code, ethnicity_code
from {{ ref('fhir_patient') }}
