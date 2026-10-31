-- One init at a time (parallel intake tasks): concurrent CREATE OR REPLACE FUNCTION and
-- GRANT on one object fail with "tuple concurrently updated". Released at commit.
SELECT pg_advisory_xact_lock(724163001);

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

-- One row per delivery: a (content, file name) pair. Content is loaded once (intake_file),
-- but a full-file feed can resend identical content under a newer name, and that newer
-- delivery's business date is what makes it the latest file. file_date is the validated
-- date in the name (NULL when the name has none). Deliveries recorded before this table
-- existed are backfilled with file_date NULL: they count by the date they were received.
CREATE TABLE IF NOT EXISTS ops.intake_delivery (
    feed              text        NOT NULL,
    file_sha256       text        NOT NULL,
    source_key        text        NOT NULL,
    file_date         date,
    received_at       timestamptz NOT NULL,
    PRIMARY KEY (feed, file_sha256, source_key),
    FOREIGN KEY (feed, file_sha256) REFERENCES ops.intake_file (feed, file_sha256)
);
-- Stage 2: how each delivery arrived (minio, sftp, api). Older rows were all MinIO.
ALTER TABLE ops.intake_delivery ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'minio';
INSERT INTO ops.intake_delivery (feed, file_sha256, source_key, received_at)
SELECT feed, file_sha256, source_key, received_at FROM ops.intake_file
ON CONFLICT DO NOTHING;

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
    status            text        NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'released', 'discarded', 'superseded')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    superseded_by     text
);
CREATE INDEX IF NOT EXISTS quarantine_reason_idx ON ops.quarantine (feed, reason_code);
-- Stage 4 (S4.5): `superseded` closes entries whose resend (an executed request_resend)
-- has loaded; superseded_by is the resent file's sha256. Added in place on older installs.
ALTER TABLE ops.quarantine ADD COLUMN IF NOT EXISTS superseded_by text;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'quarantine_status_check'
                   AND pg_get_constraintdef(oid) LIKE '%superseded%') THEN
        ALTER TABLE ops.quarantine DROP CONSTRAINT IF EXISTS quarantine_status_check;
        ALTER TABLE ops.quarantine ADD CONSTRAINT quarantine_status_check
            CHECK (status IN ('open', 'released', 'discarded', 'superseded'));
    END IF;
END $$;

-- Incidents (Stage 2 FEED.LATE in dip/sla.py, which creates the same table; Stage 7
-- FILE.QUARANTINED and DAG.FAILED in dip/incidents.py), and the incident agent's
-- diagnoses (Stage 7, report_diagnosis: a report for people and for the triage handoff;
-- it changes no data).
CREATE TABLE IF NOT EXISTS ops.incident (
    incident_id   bigserial   PRIMARY KEY,
    feed          text        NOT NULL,
    kind          text        NOT NULL,
    window_start  timestamptz NOT NULL,
    since         timestamptz NOT NULL,   -- an arrival after this closes a FEED.LATE
    opened_at     timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz,
    detail        text,
    UNIQUE (feed, kind, window_start)
);
-- The files a FILE.QUARANTINED incident is about: it closes when none of them has an open
-- file-level quarantine entry, and its handoff to triage is those files' entries.
CREATE TABLE IF NOT EXISTS ops.incident_file (
    incident_id   bigint      NOT NULL REFERENCES ops.incident (incident_id),
    file_sha256   text        NOT NULL,
    PRIMARY KEY (incident_id, file_sha256)
);
-- Stage 9 (S9.1): the agent_chain DAG's claim on an incident. One row per incident the
-- DAG picked up, inserted before the chain runs so a second DAG run never starts the
-- chain on the same incident. status: running -> done | failed | skipped (reason says why:
-- no model key in the cluster, no MCP configuration, or the error).
CREATE TABLE IF NOT EXISTS ops.agent_run (
    incident_id   bigint      PRIMARY KEY REFERENCES ops.incident (incident_id),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text        NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'done', 'failed', 'skipped')),
    reason        text,
    chain_id      text,
    root_cause    text,
    dag_run_id    text
);
-- (Stage 10, S10.1) Which provider answered each step, e.g. `incident:anthropic
-- triage:gemini`: a run that fell back to the free provider says so. Only when missing
-- (see audit_log below: ALTER locks even when the column exists).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'agent_run' AND column_name = 'provider') THEN
        ALTER TABLE ops.agent_run ADD COLUMN provider text;
    END IF;
    -- (Stage 11, S11.3) Each step's provider, model id and prompt SHA-256, e.g.
    -- {"incident": {"provider": "anthropic", "model": "...", "prompt_sha256": "..."}}.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'agent_run' AND column_name = 'versions') THEN
        ALTER TABLE ops.agent_run ADD COLUMN versions jsonb;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS ops.incident_diagnosis (
    diagnosis_id    bigserial   PRIMARY KEY,
    incident_id     bigint      NOT NULL REFERENCES ops.incident (incident_id),
    agent_id        text        NOT NULL,
    run_id          text,
    root_cause      text        NOT NULL CHECK (root_cause IN ('late_delivery',
        'quarantined_records', 'transform_failure', 'platform_failure', 'unknown')),
    summary         text        NOT NULL,
    evidence        jsonb       NOT NULL,
    quarantine_ids  jsonb       NOT NULL,
    confidence      numeric(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- Stage 9 (S9.3): the runbook the incident agent matched and the steps it proposes for a
-- person, beside its diagnosis. Steps are text for people; nothing reads them to act.
ALTER TABLE ops.incident_diagnosis ADD COLUMN IF NOT EXISTS runbook_id text;
ALTER TABLE ops.incident_diagnosis ADD COLUMN IF NOT EXISTS steps jsonb;

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

-- Accepted rows of CSV feeds (Stage 1), values exactly as received (text), keyed by
-- column name. Generic across feeds: each pack's silver models type their own feed.
CREATE TABLE IF NOT EXISTS bronze.csv_record (
    feed              text        NOT NULL,
    file_sha256       text        NOT NULL,
    row_number        integer     NOT NULL,
    record            jsonb       NOT NULL,
    loaded_at         timestamptz NOT NULL,
    PRIMARY KEY (feed, file_sha256, row_number)
);

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

-- Agent layer (PLAN.md section 7; Stage 4 triage agent). Proposals that need a human:
-- an agent writes a pending row through the Propose tier; a person approves or rejects
-- it; only an approved, unexpired row whose payload still hashes to payload_sha256 runs.
CREATE TABLE IF NOT EXISTS ops.approvals (
    approval_id       bigserial   PRIMARY KEY,
    created_at        timestamptz NOT NULL DEFAULT now(),
    agent_id          text        NOT NULL,
    run_id            text,
    action            text        NOT NULL,
    payload           jsonb       NOT NULL,
    payload_sha256    text        NOT NULL,
    status            text        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'executed', 'expired', 'failed')),
    expires_at        timestamptz NOT NULL,
    decided_by        text,
    decided_at        timestamptz,
    decision_note     text,
    executed_by       text,
    executed_at       timestamptz,
    result            jsonb
);
ALTER TABLE ops.approvals ADD COLUMN IF NOT EXISTS approved_sha256 text;
-- Stage 11 (S11.5): why a person rejected a proposal, from a fixed list (the note says the
-- rest). Frozen with the rest of the decision by the trigger below.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'approvals' AND column_name = 'decision_reason') THEN
        ALTER TABLE ops.approvals ADD COLUMN decision_reason text CHECK (decision_reason IN
            ('wrong_root_cause', 'wrong_action', 'incomplete', 'unsafe', 'other'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS approvals_status_idx ON ops.approvals (status, created_at);
-- What was proposed cannot change afterwards; only the decision and execution columns can.
CREATE OR REPLACE FUNCTION ops.approvals_payload_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload IS DISTINCT FROM OLD.payload
        OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
        OR NEW.action IS DISTINCT FROM OLD.action
        OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
        OR (OLD.approved_sha256 IS NOT NULL
            AND NEW.approved_sha256 IS DISTINCT FROM OLD.approved_sha256) THEN
        RAISE EXCEPTION 'ops.approvals: a proposal cannot be changed after it is made';
    END IF;
    -- The status moves one way (Stage 6): pending -> approved | rejected | expired, and
    -- approved -> executed | failed | expired. A decision and an execution, once recorded,
    -- stay as recorded. So no login can re-arm an approval that ran or failed, approve one
    -- without recording who and what, or mark a pending one executed.
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected', 'expired'))
        OR (OLD.status = 'approved' AND NEW.status IN ('executed', 'failed', 'expired'))
    ) THEN
        RAISE EXCEPTION 'ops.approvals: status % cannot become %', OLD.status, NEW.status;
    END IF;
    IF NEW.status = 'approved' AND OLD.status = 'pending'
        AND (NEW.decided_by IS NULL OR NEW.approved_sha256 IS NULL) THEN
        RAISE EXCEPTION 'ops.approvals: an approval records who decided and what they approved';
    END IF;
    IF NEW.status IN ('executed', 'failed') AND OLD.status = 'approved'
        AND NEW.executed_by IS NULL THEN
        RAISE EXCEPTION 'ops.approvals: an execution records who ran it';
    END IF;
    -- (Stage 9, S9.2) The whole decision record, not only who: once decided, its time and
    -- note stay too, and an approved hash is set only by the approval itself.
    IF (OLD.decided_by IS NOT NULL AND (NEW.decided_by IS DISTINCT FROM OLD.decided_by
                                        OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
                                        OR NEW.decision_note IS DISTINCT FROM OLD.decision_note
                                        OR NEW.decision_reason IS DISTINCT FROM OLD.decision_reason))
        OR (NEW.approved_sha256 IS DISTINCT FROM OLD.approved_sha256
            AND NOT (OLD.status = 'pending' AND NEW.status = 'approved'))
        OR (OLD.executed_by IS NOT NULL AND NEW.executed_by IS DISTINCT FROM OLD.executed_by)
        OR (OLD.result IS NOT NULL AND NEW.result IS DISTINCT FROM OLD.result) THEN
        RAISE EXCEPTION 'ops.approvals: a decision or an execution cannot be rewritten';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS approvals_payload_fixed ON ops.approvals;
CREATE TRIGGER approvals_payload_fixed BEFORE UPDATE ON ops.approvals
    FOR EACH ROW EXECUTE FUNCTION ops.approvals_payload_fixed();

-- Every tool call, allowed or denied, by agents and by people. Append-only through normal
-- SQL: a trigger refuses UPDATE, DELETE and TRUNCATE for every role. The table owner or a
-- superuser can still disable the trigger; running the MCP server as a role with INSERT
-- only on this table (not the owner) is the stronger setup, a follow-up.
CREATE TABLE IF NOT EXISTS ops.audit_log (
    audit_id          bigserial   PRIMARY KEY,
    at                timestamptz NOT NULL DEFAULT now(),
    principal         text        NOT NULL,
    run_id            text,
    tool              text        NOT NULL,
    tier              text        NOT NULL,
    args_sha256       text        NOT NULL,
    decision          text        NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason            text,
    result_status     text,
    latency_ms        integer
);
CREATE OR REPLACE FUNCTION ops.audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ops.audit_log is append-only (% refused)', TG_OP;
END $$;
-- (Stage 9, S9.6) What a row is about, without opening the payloads: the SHA-256 of the
-- call's result (allowed calls that returned one), the approval it proposed, decided or
-- ran, and the incident -> triage chain it belongs to. Written by Registry.audit().
-- Only when missing: ALTER takes an exclusive lock even when the column exists, and this
-- file runs on every Warehouse.init().
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'audit_log' AND column_name = 'chain_id') THEN
        ALTER TABLE ops.audit_log ADD COLUMN IF NOT EXISTS result_sha256 text,
            ADD COLUMN IF NOT EXISTS approval_id bigint, ADD COLUMN IF NOT EXISTS chain_id text;
        CREATE INDEX IF NOT EXISTS audit_log_approval_idx ON ops.audit_log (approval_id)
            WHERE approval_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS audit_log_chain_idx ON ops.audit_log (chain_id)
            WHERE chain_id IS NOT NULL;
    END IF;
END $$;
DROP TRIGGER IF EXISTS audit_log_append_only ON ops.audit_log;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON ops.audit_log
    FOR EACH ROW EXECUTE FUNCTION ops.audit_log_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON ops.audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON ops.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION ops.audit_log_append_only();

-- Stage 11 (S11.1; PLAN.md section 8.13): one row per model request an agent made, beside
-- the audit log's tool calls under the same chain id and run id. Token counts, cost (from
-- dip.agents.llm.cost_usd, the meter the spend caps use), timing and ids only: never the
-- prompt or the response text. Written by the process that runs the agents (its own
-- connection, dip/agents/model_calls.py), never by the MCP server: dip_mcp has no grant.
-- `source` is `recorded` for a replayed recording (CI), `live` for a call to a provider.
-- Append-only, like the audit log.
CREATE TABLE IF NOT EXISTS ops.model_call (
    call_id                bigserial   PRIMARY KEY,
    chain_id               text,
    run_id                 text,
    step                   text,
    provider               text        NOT NULL,
    source                 text        NOT NULL CHECK (source IN ('live', 'recorded')),
    model                  text,
    prompt_sha256          text        NOT NULL,
    turn                   integer     NOT NULL,
    status                 text        NOT NULL,
    stop_reason            text,
    input_tokens           integer     NOT NULL DEFAULT 0,
    output_tokens          integer     NOT NULL DEFAULT 0,
    cache_read_tokens      integer     NOT NULL DEFAULT 0,
    cache_creation_tokens  integer     NOT NULL DEFAULT 0,
    cost_usd               numeric(12, 6) NOT NULL DEFAULT 0,
    started_at             timestamptz NOT NULL,
    finished_at            timestamptz NOT NULL,
    latency_ms             integer     NOT NULL
);
CREATE INDEX IF NOT EXISTS model_call_chain_idx ON ops.model_call (chain_id)
    WHERE chain_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS model_call_run_idx ON ops.model_call (run_id);
CREATE INDEX IF NOT EXISTS model_call_started_idx ON ops.model_call (started_at);
CREATE OR REPLACE FUNCTION ops.model_call_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ops.model_call is append-only (% refused)', TG_OP;
END $$;
DROP TRIGGER IF EXISTS model_call_append_only ON ops.model_call;
CREATE TRIGGER model_call_append_only BEFORE UPDATE OR DELETE ON ops.model_call
    FOR EACH ROW EXECUTE FUNCTION ops.model_call_append_only();
DROP TRIGGER IF EXISTS model_call_no_truncate ON ops.model_call;
CREATE TRIGGER model_call_no_truncate BEFORE TRUNCATE ON ops.model_call
    FOR EACH STATEMENT EXECUTE FUNCTION ops.model_call_append_only();
-- Per agent run (and so per chain: sum over its run ids): calls, tokens, cost, latency.
-- Dropped and made again on every init, so a later column can go anywhere in it.
DROP VIEW IF EXISTS ops.agent_run_cost;
CREATE VIEW ops.agent_run_cost AS
SELECT chain_id, run_id, step, provider, source, max(model) AS model,
       count(*) AS calls, count(*) FILTER (WHERE status <> 'ok') AS failed_calls,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(cache_creation_tokens) AS cache_creation_tokens,
       sum(cost_usd) AS cost_usd, sum(latency_ms) AS model_ms,
       min(started_at) AS started_at, max(finished_at) AS finished_at
FROM ops.model_call
GROUP BY chain_id, run_id, step, provider, source;

-- Stage 11 (S11.5): feedback. Each agent's proposals by action and week (of the decision,
-- or of the expiry), with what people decided and why they rejected: the rate panel on the
-- Agents dashboard and the trend `dip agents feedback` prints.
CREATE OR REPLACE VIEW ops.agent_feedback AS
SELECT agent_id AS agent, action,
       date_trunc('week', coalesce(decided_at, expires_at)) AS week,
       count(*) FILTER (WHERE status IN ('approved', 'executed', 'failed')) AS approved,
       count(*) FILTER (WHERE status = 'rejected') AS rejected,
       count(*) FILTER (WHERE status = 'expired') AS expired,
       count(*) FILTER (WHERE decision_reason = 'wrong_root_cause') AS wrong_root_cause,
       count(*) FILTER (WHERE decision_reason = 'wrong_action') AS wrong_action,
       count(*) FILTER (WHERE decision_reason = 'incomplete') AS incomplete,
       count(*) FILTER (WHERE decision_reason = 'unsafe') AS unsafe,
       count(*) FILTER (WHERE decision_reason = 'other') AS other_reason
FROM ops.approvals
WHERE status <> 'pending'
GROUP BY 1, 2, 3;

-- Stage 11 (S11.2): one chain's trace. Tool spans from the audit log (a call ends when its
-- row is written, `at`, and lasted `latency_ms`) and model spans from ops.model_call, under
-- the same chain id, in time order. A view, not a pipeline (section 8.13, choice 4): an
-- OpenTelemetry exporter could read it later without any change here. Ids, names, times,
-- status, tokens and cost only, like the two tables it reads.
-- CREATE OR REPLACE: the grant survives and readers are not locked out on every init
-- (a column change later would need a DROP first).
CREATE OR REPLACE VIEW ops.trace_span AS
SELECT a.chain_id, a.run_id,
       regexp_replace(regexp_replace(a.run_id, '^.*/', ''), '[@#].*$', '') AS step,
       'tool'::text AS kind, a.tool AS name, a.principal AS actor,
       a.at - make_interval(secs => coalesce(a.latency_ms, 0) / 1000.0) AS started_at,
       a.at AS finished_at, coalesce(a.latency_ms, 0) AS duration_ms,
       CASE WHEN a.decision = 'denied' THEN 'denied' ELSE coalesce(a.result_status, 'ok') END
           AS status,
       NULL::integer AS input_tokens, NULL::integer AS output_tokens,
       NULL::numeric AS cost_usd, a.audit_id AS span_id
FROM ops.audit_log a
WHERE a.chain_id IS NOT NULL
UNION ALL
SELECT m.chain_id, m.run_id, m.step, 'model', coalesce(m.model, m.provider), m.provider,
       m.started_at, m.finished_at, m.latency_ms, m.status,
       m.input_tokens + m.cache_read_tokens + m.cache_creation_tokens, m.output_tokens,
       m.cost_usd, m.call_id
FROM ops.model_call m
WHERE m.chain_id IS NOT NULL;

-- Sample files for the contract-author agent (Stage 5): a new vendor's file, registered by
-- a person (`dip contracts sample`) before any contract exists. Its raw copy is in raw/.
CREATE TABLE IF NOT EXISTS ops.sample_file (
    feed           text        NOT NULL,
    file_sha256    text        NOT NULL,
    raw_key        text        NOT NULL,
    name           text        NOT NULL,
    registered_by  text        NOT NULL,
    registered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feed, file_sha256)
);

-- Stage 10 (S10.2): intake jobs for the multi-agent intake team (PLAN.md section 8.12).
-- One row per contract to write: `onboard` for a new vendor's sample that landed under
-- landing/_onboard/<pack>.<name>/, `drift` for a feed whose propose_contract_change a
-- person approved and executed. The agent_chain DAG claims them one at a time (like
-- ops.agent_run), or `dip contracts author` (cli) runs one directly. At most one open (queued
-- or running) job per feed.
CREATE TABLE IF NOT EXISTS ops.intake_job (
    job_id        bigserial   PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    feed          text        NOT NULL,
    mode          text        NOT NULL CHECK (mode IN ('onboard', 'drift')),
    file_sha256   text        NOT NULL,
    source        text        NOT NULL CHECK (source IN ('landing', 'handoff', 'cli')),
    source_ref    text,
    status        text        NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'proposed', 'no_change', 'revise',
                          'rejected', 'failed', 'skipped')),
    claimed_at    timestamptz,
    finished_at   timestamptz,
    dag_run_id    text,
    chain_id      text,
    provider      text,
    approval_id   bigint,
    reason        text
);
CREATE UNIQUE INDEX IF NOT EXISTS intake_job_one_open ON ops.intake_job (feed)
    WHERE status IN ('queued', 'running');
-- (S10.4) The job's profile: facts about the file built in code (dip/agents/intake_tools.py)
-- with the profile worker's labels; the only view of the file the map worker gets. And
-- (S10.5) the reviewer's verdict. Only when missing (ALTER locks even when it exists).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'intake_job' AND column_name = 'profile') THEN
        ALTER TABLE ops.intake_job ADD COLUMN profile jsonb, ADD COLUMN review jsonb;
    END IF;
    -- (Stage 11, S11.3) Each model step's provider, model id and prompt SHA-256.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'intake_job' AND column_name = 'versions') THEN
        ALTER TABLE ops.intake_job ADD COLUMN versions jsonb;
    END IF;
END $$;

-- Stage 10 (S10.4): candidate contracts of an intake job, kept by the server (they were in
-- the MCP server's memory before). The map worker's validate_contract records one; the
-- validator's validate_contract and check_dbt_models record their own verdicts on it; only
-- the intake orchestrator may propose one, and only after the validator passed it in the
-- same chain while the job runs (dip/agents/author_tools.py, propose_contract_pr).
CREATE TABLE IF NOT EXISTS ops.contract_candidate (
    chain_id         text        NOT NULL,
    candidate_id     text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    feed             text        NOT NULL,
    file_sha256      text        NOT NULL,
    contract_yaml    text        NOT NULL,
    created_by       text        NOT NULL,
    submitted        boolean     NOT NULL DEFAULT false,
    rationale        text,
    validator_valid  boolean,
    dbt_ok           boolean,
    review           jsonb,
    PRIMARY KEY (chain_id, candidate_id)
);

-- The metrics exporter's read access (S3.2, dip/metrics.py): only the ops columns its
-- queries name, on whichever of its tables exist yet. Called here, and again where
-- ops.incident (dip/sla.py) and ops.dq_results (dbt macro store_dq_results) are created.
-- Does nothing until the dip_metrics role exists (postgres-roles.sql).
CREATE OR REPLACE FUNCTION ops.grant_metrics_reader() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_metrics') THEN
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA ops TO dip_metrics;
    IF to_regclass('ops.intake_file') IS NOT NULL THEN
        GRANT SELECT (feed, status, accepted_count, rejected_count, received_at)
            ON ops.intake_file TO dip_metrics;
    END IF;
    IF to_regclass('ops.quarantine') IS NOT NULL THEN
        GRANT SELECT (feed, reason_code, status) ON ops.quarantine TO dip_metrics;
    END IF;
    IF to_regclass('ops.intake_delivery') IS NOT NULL THEN
        GRANT SELECT (feed, received_at) ON ops.intake_delivery TO dip_metrics;
    END IF;
    IF to_regclass('ops.dq_results') IS NOT NULL THEN
        GRANT SELECT (test_name, dq_category, status, failures, recorded_at)
            ON ops.dq_results TO dip_metrics;
    END IF;
    IF to_regclass('ops.incident') IS NOT NULL THEN
        GRANT SELECT (feed, kind, closed_at) ON ops.incident TO dip_metrics;
    END IF;
    -- Stage 11 (S11.4): the agents' runs, model calls and decisions; counts and times only.
    IF to_regclass('ops.model_call') IS NOT NULL THEN
        GRANT SELECT (step, run_id, provider, source, status, cost_usd, latency_ms, started_at)
            ON ops.model_call TO dip_metrics;
    END IF;
    IF to_regclass('ops.agent_run') IS NOT NULL THEN
        GRANT SELECT (status, started_at) ON ops.agent_run TO dip_metrics;
    END IF;
    IF to_regclass('ops.intake_job') IS NOT NULL THEN
        GRANT SELECT (status, created_at) ON ops.intake_job TO dip_metrics;
    END IF;
    IF to_regclass('ops.approvals') IS NOT NULL THEN
        GRANT SELECT (agent_id, status, decided_at, expires_at) ON ops.approvals TO dip_metrics;
    END IF;
END $$;
SELECT ops.grant_metrics_reader();

-- The MCP server's access (S3.4, the Read tier): what its read tools name, plus appending
-- to the audit log. Called here and where ops.dq_results is created (the dbt hook).
CREATE OR REPLACE FUNCTION ops.grant_mcp_reader() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_mcp') THEN
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA ops TO dip_mcp;
    IF to_regclass('ops.quarantine') IS NOT NULL THEN
        GRANT SELECT ON ops.quarantine TO dip_mcp;
    END IF;
    IF to_regclass('ops.intake_file') IS NOT NULL THEN
        GRANT SELECT ON ops.intake_file TO dip_mcp;
    END IF;
    IF to_regclass('ops.dq_results') IS NOT NULL THEN
        GRANT SELECT ON ops.dq_results TO dip_mcp;
    END IF;
    IF to_regclass('ops.audit_log') IS NOT NULL THEN
        GRANT INSERT ON ops.audit_log TO dip_mcp;
        GRANT USAGE ON SEQUENCE ops.audit_log_audit_id_seq TO dip_mcp;
    END IF;
    -- The incident agent (Stage 7): reads incidents, writes its diagnosis.
    IF to_regclass('ops.incident') IS NOT NULL THEN
        GRANT SELECT ON ops.incident TO dip_mcp;
    END IF;
    IF to_regclass('ops.incident_file') IS NOT NULL THEN
        GRANT SELECT ON ops.incident_file TO dip_mcp;
    END IF;
    IF to_regclass('ops.incident_diagnosis') IS NOT NULL THEN
        GRANT SELECT, INSERT ON ops.incident_diagnosis TO dip_mcp;
        GRANT UPDATE (runbook_id, steps) ON ops.incident_diagnosis TO dip_mcp;
        GRANT USAGE ON SEQUENCE ops.incident_diagnosis_diagnosis_id_seq TO dip_mcp;
    END IF;
    -- The triage agent's proposals (Stage 4, S4.5): a pending row for a person to
    -- approve. And its Act tier (Stage 6): reading an approval to check it (this login can
    -- therefore read every proposal's payload: rationales and vendor messages the triage
    -- agent wrote, from records it saw unmasked; the reader's tools never return them),
    -- and recording that it ran. Approving (decided_by, approved_sha256) stays a person's:
    -- those columns cannot be updated by this login, and the trigger above holds every
    -- login to the one-way status machine. The only action this login can complete is
    -- request_resend, which changes no data: discard and replay need writes to the
    -- quarantine and intake tables (and landing) that it does not have, so the Act check
    -- refuses them up front and a person runs them (`dip approvals execute`). Residual
    -- risk: whoever holds this login could record an approved row as executed (by any
    -- name) without running it; it could not run anything a person did not approve.
    -- Separate database logins per agent are a follow-up.
    IF to_regclass('ops.approvals') IS NOT NULL THEN
        GRANT SELECT, INSERT ON ops.approvals TO dip_mcp;
        GRANT UPDATE (status, executed_by, executed_at, result) ON ops.approvals TO dip_mcp;
        GRANT USAGE ON SEQUENCE ops.approvals_approval_id_seq TO dip_mcp;
    END IF;
    -- The intake team (Stage 10, S10.4): samples to read, the job's profile and review to
    -- record, and candidate contracts with the validator's and reviewer's verdicts. It
    -- cannot change a job's status or claim one (the intake runner does, as the warehouse
    -- owner), and nothing here writes a contract: a proposal is an approvals row.
    IF to_regclass('ops.sample_file') IS NOT NULL THEN
        GRANT SELECT ON ops.sample_file TO dip_mcp;
    END IF;
    IF to_regclass('ops.intake_job') IS NOT NULL THEN
        GRANT SELECT ON ops.intake_job TO dip_mcp;
        GRANT UPDATE (profile, review) ON ops.intake_job TO dip_mcp;
    END IF;
    IF to_regclass('ops.contract_candidate') IS NOT NULL THEN
        GRANT SELECT, INSERT ON ops.contract_candidate TO dip_mcp;
        GRANT UPDATE (submitted, rationale, validator_valid, dbt_ok, review)
            ON ops.contract_candidate TO dip_mcp;
    END IF;
END $$;
SELECT ops.grant_mcp_reader();

-- The approvals page's access (Stage 9, S9.2; the dip_approver login): list and show
-- proposals, record a person's decision, and read and append to the audit log. Only the
-- decision columns can be updated, and the trigger above still holds it to the one-way
-- status machine (pending -> approved | rejected | expired). It cannot record an
-- execution: executed_by, executed_at and result are not granted.
CREATE OR REPLACE FUNCTION ops.grant_approver() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dip_approver') THEN
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA ops TO dip_approver;
    IF to_regclass('ops.approvals') IS NOT NULL THEN
        GRANT SELECT ON ops.approvals TO dip_approver;
        GRANT UPDATE (status, decided_by, decided_at, decision_note, approved_sha256)
            ON ops.approvals TO dip_approver;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'ops'
                   AND table_name = 'approvals' AND column_name = 'decision_reason') THEN
            GRANT UPDATE (decision_reason) ON ops.approvals TO dip_approver;
        END IF;
    END IF;
    IF to_regclass('ops.audit_log') IS NOT NULL THEN
        GRANT SELECT, INSERT ON ops.audit_log TO dip_approver;
        GRANT USAGE ON SEQUENCE ops.audit_log_audit_id_seq TO dip_approver;
    END IF;
    -- (S10.1) Which provider a chain ran on, to label a proposal made on the fallback.
    IF to_regclass('ops.agent_run') IS NOT NULL THEN
        GRANT SELECT (chain_id, provider) ON ops.agent_run TO dip_approver;
    END IF;
    -- (S11.2) A chain's trace: the view only (ids, times, status, tokens, cost).
    IF to_regclass('ops.trace_span') IS NOT NULL THEN
        GRANT SELECT ON ops.trace_span TO dip_approver;
    END IF;
END $$;
SELECT ops.grant_approver();
