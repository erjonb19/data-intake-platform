-- Cluster-wide roles the platform needs, applied on every `terraform apply` by the
-- postgres-roles Job. Idempotent. The init script creates them too, but init scripts run
-- only on a fresh data volume, so a cluster created before a role was added never got it.

-- Read-only group for researchers: dbt grants it the shareable tier and nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_researcher') THEN
        CREATE ROLE dip_researcher NOLOGIN;
    END IF;
END
$$;

-- Read-only login for the clinical-drift-monitor's cohort service (R7). It is NOT a member
-- of dip_researcher: dbt grants it the shareable tier only while the warehouse holds
-- Synthea alone, classified live by the hook (macro grant_cohort_reader), and revokes
-- everything otherwise, so a MIMIC build is never readable through it. Read-only by default, short statements, few
-- connections. Its password is set by postgres-roles-password.psql from a Kubernetes
-- secret; tests set their own.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_cohort_reader') THEN
        CREATE ROLE dip_cohort_reader LOGIN;
    END IF;
END
$$;
ALTER ROLE dip_cohort_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5;
ALTER ROLE dip_cohort_reader SET default_transaction_read_only = on;
ALTER ROLE dip_cohort_reader SET statement_timeout = '60s';
ALTER ROLE dip_cohort_reader SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dip_cohort_reader SET search_path = omop_shareable;
