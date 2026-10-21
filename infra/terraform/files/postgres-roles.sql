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
