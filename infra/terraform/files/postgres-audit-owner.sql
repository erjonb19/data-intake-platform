-- 24.14: the two append-only logs, ops.audit_log (every tool call) and ops.model_call
-- (every model request, the spend caps' meter), belong to dip_audit_owner, a role nobody
-- logs in as. The `warehouse` login, which every Airflow pod holds, keeps SELECT and
-- INSERT on them and nothing else. Their triggers refuse UPDATE, DELETE and TRUNCATE for
-- every role. Only an owner can disable a trigger, replace its function, alter the table
-- or drop it, so the login that writes the rows can no longer rewrite them.
--
-- The ops schema moves to dip_audit_owner too: a schema's owner may drop any table in it.
-- `warehouse` keeps USAGE and CREATE on it, so migrations still add tables there, and
-- `warehouse` owns those tables as before.
--
-- WITH GRANT OPTION: the migrations' ops.grant_*() functions run as `warehouse` (the
-- warehouse-init Job's reapply_grants) and pass these same privileges on to the other
-- logins: INSERT for dip_mcp, dip_approver and dip_agent_runner, and SELECT for the
-- exporter and the approvals page. A grant option lets `warehouse` grant only what it holds,
-- so none of them can be given UPDATE or DELETE either.
--
-- Run as the superuser, in the warehouse database, by the postgres-audit-owner Job
-- (infra/terraform/platform_check.tf) on every apply, after warehouse-init has run the
-- migrations: the tables must exist first. Idempotent. A later migration cannot alter these
-- two tables or their trigger functions, because `warehouse` no longer owns them. A change
-- to them goes in this file (docs/migrations.md).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_audit_owner') THEN
        CREATE ROLE dip_audit_owner NOLOGIN;
    END IF;
END
$$;
ALTER ROLE dip_audit_owner WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS NOINHERIT;

ALTER SCHEMA ops OWNER TO dip_audit_owner;
GRANT USAGE, CREATE ON SCHEMA ops TO warehouse WITH GRANT OPTION;

-- The bigserial sequences and the indexes follow their table to the new owner.
ALTER TABLE ops.audit_log OWNER TO dip_audit_owner;
ALTER TABLE ops.model_call OWNER TO dip_audit_owner;
ALTER FUNCTION ops.audit_log_append_only() OWNER TO dip_audit_owner;
ALTER FUNCTION ops.model_call_append_only() OWNER TO dip_audit_owner;

-- What `warehouse` held as owner went with the ownership. It gets back only reading and
-- appending. The REVOKE names the other privileges and not ALL: on a rerun, `warehouse` has
-- passed SELECT and INSERT on to the other logins, and revoking those would fail
-- ("dependent privileges exist").
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ops.audit_log, ops.model_call
    FROM warehouse;
GRANT SELECT, INSERT ON ops.audit_log, ops.model_call TO warehouse WITH GRANT OPTION;
GRANT USAGE ON SEQUENCE ops.audit_log_audit_id_seq, ops.model_call_call_id_seq
    TO warehouse WITH GRANT OPTION;
