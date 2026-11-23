-- 13.3: the incident agent's cause for a release that never got Ready
-- (PLATFORM.ROLLOUT_STALLED), `platform_release` in dip/agents/incident_tools.py
-- ROOT_CAUSES. The check is replaced with one that takes every cause the tool offers.
ALTER TABLE ops.incident_diagnosis
    DROP CONSTRAINT IF EXISTS incident_diagnosis_root_cause_check;
ALTER TABLE ops.incident_diagnosis ADD CONSTRAINT incident_diagnosis_root_cause_check
    CHECK (root_cause IN ('late_delivery', 'quarantined_records', 'transform_failure',
    'platform_failure', 'platform_scheduler', 'platform_resources', 'platform_credentials',
    'platform_capacity', 'platform_release', 'unknown'));
