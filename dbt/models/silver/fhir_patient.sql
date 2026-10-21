select
    resource_id as patient_id,
    resource ->> 'gender' as gender,
    (resource ->> 'birthDate')::date as birth_date,
    {{ fhir_ts("resource ->> 'deceasedDateTime'") }} as deceased_at,
    {{ us_core_omb('resource', 'race') }} as race_code,
    {{ us_core_omb('resource', 'ethnicity') }} as ethnicity_code,
    resource -> 'address' -> 0 ->> 'state' as address_state,
    resource -> 'address' -> 0 ->> 'postalCode' as postal_code,
    file_sha256
from {{ ref('fhir_resource_current') }}
where resource_type = 'Patient'
