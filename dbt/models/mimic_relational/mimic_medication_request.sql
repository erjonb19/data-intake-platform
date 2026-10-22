-- MIMIC-IV prescriptions -> staging contract (the table OHDSI's MIMIC-IV ETL uses for
-- drug exposures; the FHIR demo builds its orders from `pharmacy` and POE instead, see M3).
-- prescriptions has no natural key: the id is a hash of the row, numbered within exact
-- duplicates so it stays unique and deterministic. NDC when present, else the drug name.
with numbered as (
    select
        p.*,
        row_number() over (
            partition by subject_id, hadm_id, pharmacy_id, poe_id, poe_seq, starttime, drug, ndc,
                dose_val_rx, route
            order by stoptime nulls last, prod_strength nulls last
        ) as dup_n
    from {{ source('mimic', 'prescriptions') }} as p
)

select
    md5(concat_ws('|', subject_id, hadm_id, pharmacy_id, poe_id, poe_seq, starttime, drug, ndc,
        dose_val_rx, route, dup_n)) as medication_request_id,
    subject_id::text as patient_id,
    hadm_id::text as encounter_id,
    case when nullif(ndc, '0') is not null then ndc else drug end as source_code,
    case when nullif(ndc, '0') is not null then 'NDC' else 'LOCAL' end as source_vocabulary_id,
    {{ local_to_utc('starttime') }} as authored_at,
    pharmacy_id
from numbered
