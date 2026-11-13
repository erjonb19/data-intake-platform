-- vocab.source_to_concept_map, which every dbt build reads (source_to_standard). Created
-- by a migration too, not only by a vocabulary load: a warehouse whose vocabulary predates
-- the table never reloads. The same DDL as dip.vocab.STCM_DDL (a test keeps them equal).
CREATE SCHEMA IF NOT EXISTS vocab;
CREATE TABLE IF NOT EXISTS vocab.source_to_concept_map (
    source_code varchar(50) NOT NULL, source_concept_id integer NOT NULL,
    source_vocabulary_id varchar(20) NOT NULL, source_code_description varchar(255),
    target_concept_id integer NOT NULL, target_vocabulary_id varchar(20) NOT NULL,
    valid_start_date date NOT NULL, valid_end_date date NOT NULL, invalid_reason varchar(1)
);
