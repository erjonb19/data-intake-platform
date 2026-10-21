-- Restricted: restricted person_id -> keyed pseudonym and date shift. Nobody is granted
-- access to this schema; it exists so every shareable table uses the same mapping.
{{ assert_deid_key_installed() }}

select
    p.person_id,
    {{ deid_id('p.person_source_value', 'person') }} as pseudo_person_id,
    {{ deid_shift_days('p.person_source_value') }} as shift_days
from {{ ref('person') }} as p
