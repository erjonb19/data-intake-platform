-- Warehouse tables owned by the intake core. Idempotent: safe to run on every deploy.
-- Schemas bronze, silver, gold and ops are created by infra/terraform/files/postgres-init.sh;
-- they are created here too so CI and laptops can start from an empty database.

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS ops;

-- One row per file the intake has processed. file_sha256 + feed is the idempotency key.
CREATE TABLE IF NOT EXISTS ops.intake_file (
    file_sha256       text        NOT NULL,
    feed              text        NOT NULL,
    contract_version  integer     NOT NULL,
    source_key        text        NOT NULL,
    raw_key           text        NOT NULL,
    status            text        NOT NULL CHECK (status IN ('loaded', 'loaded_with_rejections', 'quarantined')),
    accepted_count    integer     NOT NULL,
    rejected_count    integer     NOT NULL,
    skipped_count     integer     NOT NULL,
    received_at       timestamptz NOT NULL,
    PRIMARY KEY (feed, file_sha256)
);

-- One row per quarantined file (level = 'file') or record (level = 'record').
CREATE TABLE IF NOT EXISTS ops.quarantine (
    quarantine_id     bigserial   PRIMARY KEY,
    feed              text        NOT NULL,
    file_sha256       text        NOT NULL,
    level             text        NOT NULL CHECK (level IN ('file', 'record')),
    reason_code       text        NOT NULL,
    detail            text,
    record_ref        text,
    quarantine_key    text        NOT NULL,
    status            text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'released', 'discarded')),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quarantine_reason_idx ON ops.quarantine (feed, reason_code);

-- Accepted FHIR resources, as received. The silver dbt models flatten them.
CREATE TABLE IF NOT EXISTS bronze.fhir_resource (
    feed              text        NOT NULL,
    file_sha256       text        NOT NULL,
    entry_index       integer     NOT NULL,
    resource_type     text        NOT NULL,
    resource_id       text        NOT NULL,
    patient_id        text        NOT NULL,
    resource          jsonb       NOT NULL,
    loaded_at         timestamptz NOT NULL,
    PRIMARY KEY (file_sha256, entry_index)
);
CREATE INDEX IF NOT EXISTS fhir_resource_type_idx ON bronze.fhir_resource (resource_type);

-- De-identification (R4). The HMAC key lives here, not in dbt variables, so it never
-- appears in compiled SQL, dbt logs or query text. Only the warehouse owner can read it.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS ops_secret;
REVOKE ALL ON SCHEMA ops_secret FROM PUBLIC;
CREATE TABLE IF NOT EXISTS ops_secret.deid_key (
    key_id            text        PRIMARY KEY,
    key               text        NOT NULL CHECK (length(key) >= 32),
    fingerprint       text        NOT NULL,
    installed_at      timestamptz NOT NULL DEFAULT now()
);

-- Researchers get read access to the shareable tier only (granted by dbt). The role is
-- created by infra/terraform/files/postgres-init.sh in the cluster; here it is created only
-- when the connecting user may create roles (CI, laptops).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_researcher') THEN
        CREATE ROLE dip_researcher NOLOGIN;
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'cannot create role dip_researcher; expected to exist already';
END $$;
