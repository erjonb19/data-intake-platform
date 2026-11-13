-- 24.18: the metrics exporter counts refused tool calls (dip_agent_denied_calls_1h, the
-- AgentCallsDenied alert): it may read when, who and the decision of each audit row,
-- nothing else of the audit log (not the reason, the arguments' hash or the run).
-- A grant_* function, so dip.migrations.reapply_grants applies it again on every deploy
-- (a dip_metrics login created after this migration still gets it).
CREATE OR REPLACE FUNCTION ops.grant_metrics_audit() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_metrics') THEN
        GRANT SELECT (at, principal, decision) ON ops.audit_log TO dip_metrics;
    END IF;
END
$$;

SELECT ops.grant_metrics_audit();
