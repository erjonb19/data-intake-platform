"""Stage 9 (S9.1): the agent_chain DAG's task claims open incidents once, runs or skips
the chain on each, and records the outcome in ops.agent_run. The chain itself is
stubbed: what is under test is the claim and the record, not the agents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dip.agents import chain_job
from dip.incidents import open_incident
from tests.integration.conftest import rows

MCP = {"DIP_MCP_URL": "http://mcp:8765/mcp", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}


def _open(wh, feed, kind="DAG.FAILED", detail="a task failed"):
    with wh.conn.cursor() as cur:
        open_incident(cur, feed, kind, detail)
    wh.conn.commit()
    return rows(wh, "select max(incident_id) from ops.incident")[0][0]


def test_without_a_model_key_nothing_is_claimed_and_incidents_wait(warehouse):
    a = _open(warehouse, "healthcare.eligibility")

    s = chain_job.run(warehouse.conn, env=MCP, dag_run_id="manual__1")
    assert s["claimed"] == 0 and s["done"] == [] and s["failed"] == []
    assert "no model key" in s["plan"]
    assert rows(warehouse, "select count(*) from ops.agent_run") == [(0,)]

    # once a key is set, the same incident runs
    ran = chain_job.run(warehouse.conn, env={**MCP, "GEMINI_API_KEY": "g"}, runner=_ok)
    assert ran["done"] == [a]


def _ok(conn, incident_id, env, provider):
    return {"chain_id": f"chain-{incident_id}", "diagnosis": {"root_cause": "x"}}


def test_skipped_rows_and_a_dead_pods_stale_claim_are_claimed_again(warehouse):
    old = _open(warehouse, "healthcare.eligibility")
    stale = _open(warehouse, "finance.invoices")
    live = _open(warehouse, "healthcare.claims")
    with warehouse.conn.cursor() as cur:
        # `skipped`, as an earlier version recorded incidents with no key
        cur.execute(
            "insert into ops.agent_run (incident_id, status, finished_at) values"
            " (%s, 'skipped', now()), (%s, 'running', null), (%s, 'running', null)",
            (old, stale, live),
        )
        cur.execute(
            "update ops.agent_run set started_at = now() - %s where incident_id = %s",
            (chain_job.STALE_AFTER + timedelta(minutes=1), stale),
        )
    warehouse.conn.commit()

    s = chain_job.run(warehouse.conn, env={**MCP, "GEMINI_API_KEY": "g"}, runner=_ok,
                      dag_run_id="scheduled__2")  # fmt: skip
    assert s["done"] == [old, stale]  # the live claim (a running pod's) is left alone
    assert rows(
        warehouse,
        "select incident_id, status, dag_run_id from ops.agent_run order by incident_id",
    ) == [(old, "done", "scheduled__2"), (stale, "done", "scheduled__2"), (live, "running", None)]


def test_a_database_error_in_the_chain_is_recorded_and_the_next_incident_still_runs(warehouse):
    a = _open(warehouse, "healthcare.eligibility")
    b = _open(warehouse, "finance.invoices")

    def runner(conn, incident_id, env, provider):
        if incident_id == a:  # leaves the shared connection's transaction aborted
            with conn.cursor() as cur:
                cur.execute("select * from ops.no_such_table")
        return _ok(conn, incident_id, env, provider)

    s = chain_job.run(warehouse.conn, env={**MCP, "GEMINI_API_KEY": "g"}, runner=runner)
    assert s["failed"] == [a] and s["done"] == [b]
    reason = rows(warehouse, "select reason from ops.agent_run where incident_id = %s", (a,))
    assert reason[0][0].startswith("UndefinedTable")


def test_two_claimers_at_once_get_different_incidents(warehouse):
    import psycopg

    from tests.integration.conftest import DSN

    a = _open(warehouse, "healthcare.eligibility")
    b = _open(warehouse, "finance.invoices")
    with psycopg.connect(DSN) as other:
        # the first claimer holds its incident's row lock (an open transaction) ...
        with warehouse.conn.cursor() as cur:
            cur.execute("select 1 from ops.incident where incident_id = %s for update", (a,))
        # ... so the second skips it instead of waiting or taking it twice
        assert chain_job.claim_one(other)[0] == b
        warehouse.conn.rollback()
        assert chain_job.claim_one(other)[0] == a
        assert chain_job.claim_one(other) is None


def test_a_run_records_the_chain_and_a_failure_stays_on_its_incident(warehouse):
    a = _open(warehouse, "healthcare.eligibility", "FILE.QUARANTINED", "elig_20261021.csv")
    b = _open(warehouse, "finance.invoices")
    env = {**MCP, "GEMINI_API_KEY": "g"}
    calls = []

    def runner(conn, incident_id, env_, provider):
        calls.append((incident_id, provider))
        if incident_id == b:
            raise RuntimeError("mcp unreachable")
        return {
            "chain_id": "chain-abc",
            "diagnosis": {"root_cause": "quarantined_records"},
            "handoff_refused": None,
        }

    s = chain_job.run(warehouse.conn, env=env, runner=runner)
    assert calls == [(a, ("gemini",)), (b, ("gemini",))]
    assert s["done"] == [a] and s["failed"] == [b]
    got = rows(
        warehouse,
        "select incident_id, status, chain_id, root_cause, reason"
        " from ops.agent_run order by incident_id",
    )
    assert got[0] == (a, "done", "chain-abc", "quarantined_records", None)
    assert got[1][:2] == (b, "failed") and got[1][4].startswith("RuntimeError: mcp unreachable")
    # neither is claimed again, including the failed one (a person re-runs it)
    assert chain_job.run(warehouse.conn, env=env, runner=runner)["claimed"] == 0
    assert len(calls) == 2


def test_closed_incidents_are_not_claimed_and_the_limit_holds(warehouse):
    ids = [_open(warehouse, f"finance.feed{i}") for i in range(4)]
    with warehouse.conn.cursor() as cur:
        cur.execute(
            "update ops.incident set closed_at = %s where incident_id = %s",
            (datetime.now(UTC), ids[0]),
        )
    warehouse.conn.commit()
    first = chain_job.claim(warehouse.conn, limit=2)
    assert [i for i, _, _ in first] == ids[1:3]
    assert [(f, k) for _, f, k in first] == [
        ("finance.feed1", "DAG.FAILED"),
        ("finance.feed2", "DAG.FAILED"),
    ]
    second = chain_job.claim(warehouse.conn, limit=2)
    assert [i for i, _, _ in second] == [ids[3]]
    assert chain_job.claim(warehouse.conn, limit=2) == []


def test_the_row_says_which_provider_answered_each_step(warehouse):
    """S10.1: a chain that fell back to the free provider says so on its row."""
    a = _open(warehouse, "healthcare.eligibility", "FILE.QUARANTINED", "elig_20261021.csv")

    def runner(conn, incident_id, env_, providers):
        assert providers == ("anthropic", "gemini")
        return {"chain_id": "chain-fb", "diagnosis": {"root_cause": "quarantined_records"},
                "providers": {"incident": "anthropic", "triage": "gemini"}}  # fmt: skip

    env = {**MCP, "ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}
    assert chain_job.run(warehouse.conn, env=env, runner=runner)["done"] == [a]
    assert rows(warehouse, "select provider from ops.agent_run") == [
        ("incident:anthropic triage:gemini",)]  # fmt: skip


def test_when_no_provider_answers_the_incident_waits_and_the_run_stops(warehouse):
    """S10.1: every provider down before anything was written is an outage, not a failure
    of the incident: it goes back to `skipped` (claimed again later), and this run stops."""
    from dip.agents.llm import NoProvider

    a = _open(warehouse, "healthcare.eligibility", "FILE.QUARANTINED", "elig_20261021.csv")
    _open(warehouse, "finance.invoices")
    calls = []

    def runner(conn, incident_id, env_, providers):
        calls.append(incident_id)
        exc = NoProvider("anthropic: 529; gemini: 503")
        exc.step = "incident"
        raise exc

    s = chain_job.run(warehouse.conn, env={**MCP, "GEMINI_API_KEY": "g"}, runner=runner)
    assert calls == [a] and s["skipped"] == [a] and s["failed"] == []
    got = rows(warehouse, "select status, reason from ops.agent_run where incident_id = %s", (a,))
    assert got[0][0] == "skipped" and "no provider answered" in got[0][1]


def test_each_steps_provider_is_on_the_row_as_soon_as_it_finishes(warehouse):
    a = _open(warehouse, "healthcare.eligibility", "FILE.QUARANTINED", "elig_20261021.csv")
    assert chain_job.claim_one(warehouse.conn)[0] == a
    chain_job.record_step(warehouse.conn, a, "incident", "gemini")
    assert rows(warehouse, "select status, provider from ops.agent_run") == [
        ("running", "incident:gemini")]  # fmt: skip
    chain_job.record_step(warehouse.conn, a, "triage", "anthropic")
    chain_job.finish(warehouse.conn, a, "failed", reason="later crash")
    assert rows(warehouse, "select status, provider from ops.agent_run") == [
        ("failed", "incident:gemini triage:anthropic")]  # fmt: skip
