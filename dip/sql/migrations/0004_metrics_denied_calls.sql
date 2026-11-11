-- 24.18: the metrics exporter counts refused tool calls (dip_agent_denied_calls_1h, the
-- AgentCallsDenied alert): it may read when, who and the decision of each audit row,
-- nothing else of the audit log (not the reason, the arguments' hash or the run).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_metrics') THEN
        GRANT SELECT (at, principal, decision) ON ops.audit_log TO dip_metrics;
    END IF;
END
$$;
