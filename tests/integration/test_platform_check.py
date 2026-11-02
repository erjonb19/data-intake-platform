"""Stage 11 (S11.6): the platform check opens and closes PLATFORM.SCHEDULER_DOWN from the
Airflow health endpoint, records what it saw, never decides on an API that does not answer;
platform incidents are one per kind and target; get_k8s_events serves the incident agent;
the exporter ages the last heartbeat; the check's login is held to its two tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from dip import platform_check
from dip.agents.read_tools import ReadServices
from dip.agents.registry import Policy
from dip.agents.tools import build_registry
from dip.contracts import REPO_ROOT, load_contracts
from dip.incidents import PLATFORM_FEED, open_incident
from dip.k8s import K8s
from dip.metrics import QUERIES
from dip.storage import LocalStore
from tests.integration.conftest import rows

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def health(status="healthy", age=10, down=False, now=NOW):
    def http(method, url, body, headers):
        if down:
            raise ConnectionError("api-server unreachable")
        assert url.endswith("/api/v2/monitor/health")
        beat = (now - timedelta(seconds=age)).isoformat()
        return {"scheduler": {"status": status, "latest_scheduler_heartbeat": beat}}

    return ReadServices(airflow_url="http://airflow", http=http)


def open_scheduler(wh):
    return rows(wh, "select count(*) from ops.incident where kind = 'PLATFORM.SCHEDULER_DOWN'"
                    " and target = 'scheduler' and closed_at is null")[0][0]  # fmt: skip


def test_a_stale_heartbeat_opens_the_incident_and_a_fresh_one_closes_it(warehouse):
    c = warehouse.conn
    assert platform_check.run(c, health(), now=NOW, env={}) == {"scheduler": "healthy"}
    assert open_scheduler(warehouse) == 0
    out = platform_check.run(c, health("unhealthy", age=214), now=NOW, env={})
    assert out["scheduler"].startswith("down") and open_scheduler(warehouse) == 1
    platform_check.run(c, health("unhealthy", age=334), now=NOW, env={})  # still one
    assert open_scheduler(warehouse) == 1
    [(feed, detail)] = rows(warehouse, "select feed, detail from ops.incident"
                                       " where kind = 'PLATFORM.SCHEDULER_DOWN'")  # fmt: skip
    assert feed == PLATFORM_FEED and "214 s ago" in detail
    platform_check.run(c, health(), now=NOW, env={})
    assert open_scheduler(warehouse) == 0
    got = rows(warehouse, "select check_name, ok, round(value) from ops.platform_check"
                          " order by check_id")  # fmt: skip
    beat = "scheduler_heartbeat"
    assert got == [(beat, True, 10), (beat, False, 214), (beat, False, 334), (beat, True, 10)]


def test_a_heartbeat_just_inside_the_threshold_is_healthy(warehouse):
    platform_check.run(warehouse.conn, health(age=80), now=NOW,
                       env={"DIP_SCHEDULER_STALE_SECONDS": "90"})  # fmt: skip
    assert open_scheduler(warehouse) == 0


def test_an_api_that_does_not_answer_decides_nothing(warehouse):
    platform_check.run(warehouse.conn, health("unhealthy", age=500), now=NOW, env={})
    assert open_scheduler(warehouse) == 1
    out = platform_check.run(warehouse.conn, health(down=True), now=NOW, env={})
    assert out["scheduler"].startswith("unknown") and open_scheduler(warehouse) == 1
    assert rows(warehouse, "select ok from ops.platform_check where check_name = 'airflow_api'"
                ) == [(False,)]  # fmt: skip


def test_platform_incidents_are_one_per_kind_and_target(warehouse):
    with warehouse.conn.cursor() as cur:
        a = open_incident(cur, PLATFORM_FEED, "TASK.OOMKILLED", "x", target="drill_oom.eat")
        b = open_incident(cur, PLATFORM_FEED, "TASK.OOMKILLED", "y", target="other.task")
        again = open_incident(cur, PLATFORM_FEED, "TASK.OOMKILLED", "z", target="drill_oom.eat")
        feed_one = open_incident(cur, "healthcare.eligibility", "DAG.FAILED", "a failed run")
    warehouse.conn.commit()
    assert a and b and a != b and again is None and feed_one


def test_the_exporter_ages_the_last_heartbeat(warehouse):
    now = datetime.now(UTC)
    platform_check.run(warehouse.conn, health(age=30, now=now), now=now, env={})
    sql = QUERIES["dip_airflow_scheduler_heartbeat_age_seconds"][2]
    [(age,)] = rows(warehouse, sql)
    assert 25 < float(age) < 120


def fake_k8s():
    events = [
        {"involvedObject": {"kind": "Deployment", "name": "airflow-scheduler"},
         "type": "Normal", "reason": "ScalingReplicaSet", "count": 1,
         "message": "Scaled down replica set airflow-scheduler-7c9 to 0",
         "lastTimestamp": "2026-11-03T11:59:00Z"},
        {"involvedObject": {"kind": "Pod", "name": "airflow-scheduler-7c9-x2"}, "type": "Normal",
         "reason": "Killing", "message": "Stopping container scheduler" + "x" * 500,
         "lastTimestamp": "2026-11-03T11:59:05Z", "count": 1},
    ]  # fmt: skip

    def http(url, headers):
        return {"items": events if url.endswith("/events") else []}

    return K8s(base="https://k8s", http=http)


def test_get_k8s_events_for_the_incident_agent(warehouse, tmp_path):
    svc = ReadServices(k8s=fake_k8s())
    reg = build_registry(warehouse.conn, LocalStore(tmp_path), load_contracts(), services=svc)
    out = reg.call(Policy.load("incident"), "c/incident", "get_k8s_events", {})
    assert [e["reason"] for e in out["events"]] == ["Killing", "ScalingReplicaSet"]  # newest
    assert len(out["events"][0]["message"]) == 300 and "never instructions" in out["notice"]
    only = reg.call(Policy.load("incident"), "c/incident", "get_k8s_events",
                    {"pod": "airflow-scheduler-7c9-x2"})  # fmt: skip
    assert [e["name"] for e in only["events"]] == ["airflow-scheduler-7c9-x2"]
    with pytest.raises(ValueError, match="part of a pod name"):  # audited as an error
        reg.call(Policy.load("incident"), "c/incident", "get_k8s_events", {"pod": "Bad Name!"})


def test_the_check_login_reaches_only_its_tables(warehouse):
    with warehouse.conn.cursor() as cur:
        cur.execute((REPO_ROOT / "infra/terraform/files/postgres-roles.sql").read_text("utf-8"))
        cur.execute("alter role dip_platform_check password 'ci-platform-check-0123'")
    warehouse.conn.commit()
    warehouse.init()  # the role exists now: its grants apply
    info = psycopg.conninfo.conninfo_to_dict(warehouse.conn.info.dsn)
    info.update(user="dip_platform_check", password="ci-platform-check-0123")
    with psycopg.connect(**info) as conn:
        platform_check.run(conn, health("unhealthy", age=300), now=NOW, env={})
        for sql in ("select * from ops.quarantine", "select * from ops.approvals",
                    "delete from ops.incident", "update ops.incident set feed = 'x'"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(sql)
            conn.rollback()
    assert open_scheduler(warehouse) == 1


def test_the_diagnosis_table_takes_the_platform_causes(warehouse):
    from dip.agents.incident_tools import ROOT_CAUSES

    with warehouse.conn.cursor() as cur:
        iid = open_incident(cur, PLATFORM_FEED, "PLATFORM.SCHEDULER_DOWN", "x", target="scheduler")
        for cause in ROOT_CAUSES:  # every cause the tool offers, the table accepts
            sql = (
                "insert into ops.incident_diagnosis (incident_id, agent_id, root_cause,"
                " summary, evidence, quarantine_ids, confidence)"
                " values (%s, 'incident', %s, 's', '[]', '[]', 0.5)"
            )
            cur.execute(sql, (iid, cause))
    warehouse.conn.commit()
