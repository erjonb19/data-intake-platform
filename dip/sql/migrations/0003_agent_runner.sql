-- 24.9: the scheduled agent runners' own login, instead of the warehouse owner's.
-- `dip.agents.chain_job` and `dip.agents.intake_runner` (the agent_chain DAG, in the agent
-- venv) claim and finish their work and record what they did; everything that changes data
-- goes through the MCP server, under its own login and policy. So this login may:
--   read     the incident, its diagnosis, files and quarantine (the handoff), the audit
--            log (which tool calls succeeded), onboarding jobs and contract candidates,
--            ops.model_call (the monthly spend cap, 24.3), ops.schema_migration
--            (Warehouse.init's check) and its own agent_run rows
--   write    ops.agent_run (claim, finish, versions), ops.intake_job's run columns,
--            ops.model_call and ops.audit_log (inserts only)
-- and nothing in bronze, silver, gold, the research tiers or ops_secret.
--
-- `for update skip locked` on an incident (chain_job.claim_one) needs UPDATE on one of its
-- columns: `detail`, the free text, is the one granted. The role is created by
-- infra/terraform/files/postgres-roles.sql; without it this grants nothing.
CREATE OR REPLACE FUNCTION ops.grant_agent_runner() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_agent_runner') THEN
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA ops TO dip_agent_runner;
    GRANT SELECT ON ops.incident, ops.incident_diagnosis, ops.incident_file, ops.quarantine,
        ops.audit_log, ops.intake_job, ops.contract_candidate, ops.model_call,
        ops.schema_migration TO dip_agent_runner;
    GRANT UPDATE (detail) ON ops.incident TO dip_agent_runner;
    GRANT SELECT, INSERT, UPDATE ON ops.agent_run TO dip_agent_runner;
    GRANT UPDATE (status, claimed_at, finished_at, dag_run_id, reason, chain_id, approval_id,
        provider, versions, profile, review) ON ops.intake_job TO dip_agent_runner;
    GRANT INSERT ON ops.model_call, ops.audit_log TO dip_agent_runner;
    GRANT USAGE ON SEQUENCE ops.model_call_call_id_seq, ops.audit_log_audit_id_seq
        TO dip_agent_runner;
END
$$;

SELECT ops.grant_agent_runner();
