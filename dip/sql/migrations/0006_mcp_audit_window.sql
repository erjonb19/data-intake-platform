-- 24.31: the MCP server's hourly call limits survive a restart. Its registry starts each
-- agent's hourly window from the audit log's allowed calls in the last hour
-- (dip/agents/registry.py, Registry._seed), so its login may read when, who, which tool and
-- the decision of each audit row. Not the reason, the arguments' hash, the run or the
-- result. It still appends rows and can change none (24.14).
-- A grant_* function, so dip.migrations.reapply_grants applies it again on every deploy.
CREATE OR REPLACE FUNCTION ops.grant_mcp_audit_window() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_mcp') THEN
        GRANT SELECT (at, principal, tool, decision) ON ops.audit_log TO dip_mcp;
    END IF;
END
$$;

SELECT ops.grant_mcp_audit_window();
