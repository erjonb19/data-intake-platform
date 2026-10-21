-- Latest copy of each FHIR resource. A patient may be re-exported in a newer file; the
-- newest load wins, with the file hash as a deterministic tie-break.
select resource_type, resource_id, patient_id, resource, file_sha256
from (
    select
        r.*,
        row_number() over (
            partition by resource_type, resource_id
            order by loaded_at desc, file_sha256 desc
        ) as version_rank
    from {{ source('bronze', 'fhir_resource') }} as r
) as ranked
where version_rank = 1
