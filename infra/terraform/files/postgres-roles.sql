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

-- Read-only login for the warehouse metrics exporter (S3.2, dip/metrics.py). It reads only
-- the ops columns its queries name: ops.grant_metrics_reader() (dip/sql/warehouse.sql)
-- grants them where each table is created. Nothing in bronze, silver, gold or the research
-- tiers. Its password is set by postgres-roles-password.psql from a Kubernetes secret.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_metrics') THEN
        CREATE ROLE dip_metrics LOGIN;
    END IF;
END
$$;
ALTER ROLE dip_metrics WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 3;
ALTER ROLE dip_metrics SET default_transaction_read_only = on;
ALTER ROLE dip_metrics SET statement_timeout = '10s';
ALTER ROLE dip_metrics SET search_path = ops;

-- The MCP server's login (S3.4, the Read tier, infra/terraform/mcp.tf). It reads the ops
-- tables its read tools name and appends to ops.audit_log (every call is audited), by the
-- grants in ops.grant_mcp_reader() (dip/sql/warehouse.sql). Not read-only by default: the
-- audit insert is a write. Nothing in bronze, silver, gold or the research tiers.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_mcp') THEN
        CREATE ROLE dip_mcp LOGIN;
    END IF;
END
$$;
ALTER ROLE dip_mcp WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5;
ALTER ROLE dip_mcp SET statement_timeout = '15s';
ALTER ROLE dip_mcp SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dip_mcp SET search_path = ops;

-- The approvals page's login (Stage 9, S9.2, infra/terraform/approvals.tf): reads
-- ops.approvals and ops.audit_log, appends to the audit log, and sets an approval's
-- decision columns, by the grants in ops.grant_approver() (dip/sql/warehouse.sql). It
-- cannot execute an approval or touch any data table.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_approver') THEN
        CREATE ROLE dip_approver LOGIN;
    END IF;
END
$$;
ALTER ROLE dip_approver WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5;
ALTER ROLE dip_approver SET statement_timeout = '15s';
ALTER ROLE dip_approver SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dip_approver SET search_path = ops;

-- The platform check's login (Stage 11, S11.6; dip/platform_check.py, a CronJob in
-- dip-airflow). It opens and closes platform incidents and records what it saw: insert and
-- update on ops.incident (the decision columns only) and insert on ops.platform_check, by
-- ops.grant_platform_check() (dip/sql/warehouse.sql). Nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_platform_check') THEN
        CREATE ROLE dip_platform_check LOGIN;
    END IF;
END
$$;
ALTER ROLE dip_platform_check WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT CONNECTION LIMIT 3;
ALTER ROLE dip_platform_check SET statement_timeout = '15s';
ALTER ROLE dip_platform_check SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dip_platform_check SET search_path = ops;
