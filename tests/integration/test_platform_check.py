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


def health(status="healthy", age=10, down=False, now=NOW, runs=(), waiting=None, paused=()):
    def http(method, url, body, headers):
        if down:
            raise ConnectionError("api-server unreachable")
        if url.endswith("/auth/token"):  # S11.8: the dag runs need the Viewer login
            return {"access_token": "viewer"}
        if "/dags/~/dagRuns?" in url:  # newest first
            assert headers.get("Authorization") == "Bearer viewer"
            return {"dag_runs": list(runs)}
        if "/dags?paused=true" in url:  # S11.9: a paused DAG's scheduled tasks are held
            return {"dags": [{"dag_id": d} for d in paused]}
        if "/dags/~/dagRuns/~/taskInstances?" in url:  # S11.9: tasks still waiting
            assert "state=scheduled&state=queued" in url
            tis = list(waiting or ())
            return {"task_instances": tis, "total_entries": len(tis)}
        assert url.endswith("/api/v2/monitor/health")
        beat = (now - timedelta(seconds=age)).isoformat()
        return {"scheduler": {"status": status, "latest_scheduler_heartbeat": beat}}

    return ReadServices(airflow_url="http://airflow", airflow_user="viewer",
                        airflow_password="pw", http=http)  # fmt: skip


def open_scheduler(wh):
    return rows(wh, "select count(*) from ops.incident where kind = 'PLATFORM.SCHEDULER_DOWN'"
                    " and target = 'scheduler' and closed_at is null")[0][0]  # fmt: skip


def test_a_stale_heartbeat_opens_the_incident_and_a_fresh_one_closes_it(warehouse):
    c = warehouse.conn
    out = platform_check.run(c, health(), now=NOW, env={})
    assert out == {"scheduler": "healthy", "dag_runs_failed": [], "stuck_queued": []}
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
    assert rows(warehouse, "select target, ok from ops.platform_check"
                           " where check_name = 'airflow_api' order by target"
                ) == [("api-server", False), ("dag-runs", False),
                      ("task-instances", False)]  # fmt: skip


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


def oom_pods(present: bool):
    pod = {"metadata": {"name": "drill-oom-eat-memory-4c1d2",
                        "labels": {"dag_id": "drill_oom", "task_id": "eat_memory"}},
           "spec": {"containers": [{"name": "base", "resources": {"limits": {"memory": "128Mi"}}}]},
           "status": {"containerStatuses": [{"name": "base", "state": {"terminated": {
               "reason": "OOMKilled", "exitCode": 137,
               "finishedAt": "2026-11-04T10:00:21Z"}}}]}}  # fmt: skip

    def http(url, headers):
        return {"items": [pod] if present and url.endswith("/pods") else []}

    return K8s(base="https://k8s", http=http)


def test_an_oomkilled_pod_opens_its_tasks_incident_until_it_is_gone(warehouse):
    def open_oom():
        return rows(warehouse, "select target from ops.incident where kind = 'TASK.OOMKILLED'"
                               " and closed_at is null")  # fmt: skip

    out = platform_check.run(warehouse.conn, health(), oom_pods(True), now=NOW, env={})
    assert out["oomkilled"] == ["drill_oom.eat_memory"]
    assert open_oom() == [("drill_oom.eat_memory",)]
    [(detail,)] = rows(warehouse, "select detail from ops.incident where kind = 'TASK.OOMKILLED'")
    assert "exit 137" in detail and "128Mi" in detail
    platform_check.run(warehouse.conn, health(), oom_pods(True), now=NOW, env={})  # still one
    assert len(open_oom()) == 1
    platform_check.run(warehouse.conn, health(), oom_pods(False), now=NOW, env={})
    assert open_oom() == []


def test_an_oomkilled_restart_outside_airflow_tasks_opens_nothing(warehouse):
    # The scheduler's container was OOMKilled once and restarted: its pod keeps that in
    # lastState, with no dag_id/task_id labels. Not a task's incident (review of S11.7).
    pod = {"metadata": {"name": "airflow-scheduler-7d9f", "labels": {"component": "scheduler"}},
           "status": {"containerStatuses": [{
               "name": "scheduler", "state": {"running": {}},
               "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137,
                                            "finishedAt": "2026-11-04T09:00:00Z"}}}]}}  # fmt: skip

    def http(url, headers):
        return {"items": [pod] if url.endswith("/pods") else []}

    out = platform_check.run(warehouse.conn, health(), K8s(base="https://k8s", http=http),
                             now=NOW, env={})  # fmt: skip
    assert out["oomkilled"] == []
    assert rows(warehouse, "select 1 from ops.incident where kind = 'TASK.OOMKILLED'") == []


def dag_run(state, run_id, dag="intake__healthcare__eligibility", end="2026-11-03T11:58:00Z"):
    return {"dag_id": dag, "dag_run_id": run_id, "state": state, "end_date": end}


def test_a_failed_feed_run_opens_dag_failed_without_its_callback(warehouse):
    # S11.8: a bad secret breaks the run's own callback (it needs the warehouse); the
    # check, with its own login, opens the feed's DAG.FAILED, once, and a later
    # successful run closes it. A drill DAG's failure is not a feed's.
    def failed_open():
        return rows(warehouse, "select feed, target from ops.incident"
                               " where kind = 'DAG.FAILED' and closed_at is null")  # fmt: skip

    failed = [dag_run("failed", "manual__2"), dag_run("success", "manual__1"),
              dag_run("failed", "manual__9", dag="drill_oom")]  # fmt: skip
    out = platform_check.run(warehouse.conn, health(runs=failed), now=NOW, env={})
    assert out["dag_runs_failed"] == ["intake__healthcare__eligibility"]
    assert failed_open() == [("healthcare.eligibility", "intake__healthcare__eligibility")]
    platform_check.run(warehouse.conn, health(runs=failed), now=NOW, env={})  # still one
    assert len(failed_open()) == 1
    # the failed run cleared to rerun: queued, then running; nothing has succeeded yet
    rerun = [dag_run("queued", "manual__2", end=None), dag_run("success", "manual__1")]
    platform_check.run(warehouse.conn, health(runs=rerun), now=NOW, env={})
    assert len(failed_open()) == 1
    ok = [dag_run("success", "manual__3"), *failed]
    platform_check.run(warehouse.conn, health(runs=ok), now=NOW, env={})
    assert failed_open() == []


def test_the_check_adds_nothing_to_the_callbacks_incident(warehouse):
    from dip.incidents import dag_callback

    class Run:
        run_id, dag_id = "manual__2", "intake__healthcare__eligibility"

    from tests.integration.conftest import DSN

    dag_callback("healthcare.eligibility", True, {"dag_run": Run()}, lambda: psycopg.connect(DSN))
    platform_check.run(warehouse.conn, health(runs=[dag_run("failed", "manual__2")]),
                       now=NOW, env={})  # fmt: skip
    assert rows(warehouse, "select target from ops.incident where kind = 'DAG.FAILED'") == [
        ("intake__healthcare__eligibility",)]  # fmt: skip


def waiting(state, waited_s, dag="drill_queue", task="wait_for_room", run="manual__1"):
    since = (NOW - timedelta(seconds=waited_s)).isoformat()
    when = {"queued_when": since} if state == "queued" else {"scheduled_when": since}
    return {
        "dag_id": dag,
        "task_id": task,
        "dag_run_id": run,
        "state": state,
        "pool": "default_pool",
        "queued_when": None,
        "scheduled_when": None,
    } | when


def open_stuck(wh):
    return rows(wh, "select target from ops.incident where kind = 'TASK.STUCK_QUEUED'"
                    " and closed_at is null order by target")  # fmt: skip


def test_a_task_waiting_past_the_threshold_opens_stuck_queued_until_it_starts(warehouse):
    """S11.9: a task scheduled or queued longer than DIP_QUEUED_STALE_SECONDS opens
    TASK.STUCK_QUEUED for its DAG and task, once; it closes when nothing of it waits."""
    env = {"DIP_QUEUED_STALE_SECONDS": "120"}
    young = [waiting("queued", 60)]
    out = platform_check.run(warehouse.conn, health(waiting=young), now=NOW, env=env)
    assert out["stuck_queued"] == [] and open_stuck(warehouse) == []
    stuck = [waiting("queued", 154), waiting("scheduled", 400, dag="intake__x", task="intake")]
    out = platform_check.run(warehouse.conn, health(waiting=stuck), now=NOW, env=env)
    assert out["stuck_queued"] == ["drill_queue.wait_for_room", "intake__x.intake"]
    assert open_stuck(warehouse) == [("drill_queue.wait_for_room",), ("intake__x.intake",)]
    [(detail,)] = rows(
        warehouse,
        "select detail from ops.incident where kind ="
        " 'TASK.STUCK_QUEUED' and target = 'drill_queue.wait_for_room'",
    )
    assert "queued for 154 s" in detail and "default_pool" in detail
    platform_check.run(warehouse.conn, health(waiting=stuck), now=NOW, env=env)  # still one each
    assert len(open_stuck(warehouse)) == 2
    platform_check.run(warehouse.conn, health(waiting=stuck[1:]), now=NOW, env=env)
    assert open_stuck(warehouse) == [("intake__x.intake",)]
    got = rows(warehouse, "select target, ok, round(value) from ops.platform_check where"
                          " check_name = 'stuck_queued' order by check_id limit 1")  # fmt: skip
    assert got == [("drill_queue.wait_for_room", False, 154)]


def test_the_default_threshold_is_five_minutes_and_a_partial_page_closes_nothing(warehouse):
    out = platform_check.run(warehouse.conn, health(waiting=[waiting("queued", 280)]),
                             now=NOW, env={})  # fmt: skip
    assert out["stuck_queued"] == []
    platform_check.run(warehouse.conn, health(waiting=[waiting("queued", 320)]), now=NOW, env={})
    assert open_stuck(warehouse) == [("drill_queue.wait_for_room",)]

    def more_than_a_page(method, url, body, headers):
        if "/taskInstances?" in url:
            return {"task_instances": [], "total_entries": 250}
        return health().http(method, url, body, headers)

    svc = ReadServices(airflow_url="http://airflow", airflow_user="viewer",
                       airflow_password="pw", http=more_than_a_page)  # fmt: skip
    platform_check.run(warehouse.conn, svc, now=NOW, env={})
    assert open_stuck(warehouse) == [("drill_queue.wait_for_room",)]


def test_task_instances_the_api_cannot_list_decide_nothing(warehouse):
    platform_check.run(warehouse.conn, health(waiting=[waiting("queued", 900)]), now=NOW, env={})

    def no_tis(method, url, body, headers):
        if "/taskInstances?" in url:
            raise ConnectionError("api-server unreachable")
        return health().http(method, url, body, headers)

    svc = ReadServices(airflow_url="http://airflow", airflow_user="viewer",
                       airflow_password="pw", http=no_tis)  # fmt: skip
    out = platform_check.run(warehouse.conn, svc, now=NOW, env={})
    assert out["stuck_queued"].startswith("unknown")
    assert open_stuck(warehouse) == [("drill_queue.wait_for_room",)]


def test_a_paused_dags_scheduled_task_is_held_not_stuck(warehouse):
    """Review of S11.9: the scheduler never queues a paused DAG's scheduled task, so it is
    not stuck (it would never close); a queued one of a paused DAG still is (the executor
    has it). A scheduled retry is timed from scheduled_when, not an earlier try's
    queued_when."""
    held = waiting("scheduled", 900, dag="intake__x", task="intake")
    queued = waiting("queued", 900)
    retry = waiting("scheduled", 30, dag="intake__y", task="intake") | {
        "queued_when": (NOW - timedelta(seconds=3600)).isoformat()
    }
    out = platform_check.run(warehouse.conn, health(waiting=[held, queued, retry],
                             paused=["intake__x", "drill_queue"]), now=NOW, env={})  # fmt: skip
    assert out["stuck_queued"] == ["drill_queue.wait_for_room"]
