"""The intake team's guardrails on the server (Stage 10, S10.4; PLAN.md section 8.12), over
the real MCP server and policies: what each member may call, and what the server checks
before the orchestrator's proposal becomes an approvals row. The run id is the client's,
so every check that matters rests on what the server recorded under another member's key."""

from __future__ import annotations

import asyncio
import json

import pytest

from dip.agents import author_evals, triage
from dip.agents.evals import LocalServer
from dip.intake_jobs import claim_one, finish, queue
from tests.integration.conftest import DSN, rows

CASE = {c["name"]: c for c in author_evals.load_cases()}["drift_new_plan_code"]
MEMBERS = ("profile", "contract_author", "validator", "reviewer", "intake_orchestrator")


def _candidate() -> str:
    text = (author_evals.EVALS / "contracts" / "eligibility.yaml").read_text("utf-8")
    return text.replace("[HMO, PPO, EPO, HDHP]", "[HMO, PPO, EPO, HDHP, GOLD]").replace(
        "version: 1", "version: 2")  # fmt: skip


@pytest.fixture
def world(warehouse, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    store, sha = author_evals.build_state(warehouse.conn, tmp_path, CASE)
    with warehouse.conn.cursor() as cur:
        queue(cur, CASE["feed"], "drift", sha, "cli")
    warehouse.conn.commit()
    job = claim_one(warehouse.conn)
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.intake_job set chain_id = 'c1' where job_id = %s",
                    (job["job_id"],))  # fmt: skip
    warehouse.conn.commit()
    with LocalServer(warehouse.conn, store, agents=MEMBERS,
                     contracts=author_evals.eval_contracts()) as srv:  # fmt: skip

        def call(member: str, tool: str, run_id: str, **args):
            async def go():
                async with triage.mcp_client(srv.url, srv.keys[member]) as client:
                    r = await client.call_tool(tool, {**args, "_run_id": run_id})
                    text = "\n".join(getattr(c, "text", "") for c in r.content)
                    return r.is_error, text

            err, text = asyncio.run(go())
            try:
                return err, json.loads(text)
            except ValueError:
                return err, {"error": text}

        yield call, job, sha


def test_only_a_validated_submitted_candidate_of_a_running_job_is_proposed(world, warehouse):
    call, job, sha = world
    feed, jid = CASE["feed"], job["job_id"]
    # The profile worker labels; the map worker sees the profile only in this job's chain.
    err, out = call("profile", "report_profile", "c1/profile", job_id=jid,
                    columns=[{"column": "plan_code", "phi": False, "role": "code"}])  # fmt: skip
    assert not err and out["labelled"] == 1, out
    err, got = call("contract_author", "get_intake_profile", "c1/map", job_id=jid)
    assert not err and got["profile"]["columns"]
    err, other = call("contract_author", "get_intake_profile", "c2/map", job_id=jid)
    assert "another chain" in json.dumps(other)

    # Nothing submitted yet: refused. (Each attempt is its own run, `#n`: the orchestrator's
    # policy allows one proposal call per run, refused or not.)
    err, out = call("intake_orchestrator", "propose_contract_pr", "c1/propose#1",
                    candidate_id="0" * 16, mode="drift", rationale="x")  # fmt: skip
    assert "no candidate" in json.dumps(out)
    # The map worker validates and submits, but the validator has not passed it: refused.
    err, v = call("contract_author", "validate_contract", "c1/map", feed=feed,
                  contract_yaml=_candidate(), file_sha256=sha)  # fmt: skip
    cid = v["candidate_id"]
    err, s = call("contract_author", "submit_candidate", "c1/map", candidate_id=cid,
                  rationale="GOLD is a new plan.")  # fmt: skip
    assert s["submitted"] is True
    err, out = call("intake_orchestrator", "propose_contract_pr", "c1/propose#2",
                    candidate_id=cid, mode="drift", rationale="x")  # fmt: skip
    assert "validator has not passed" in json.dumps(out)
    # The map worker may not propose at all (its policy), whatever it claims.
    err, out = call("contract_author", "propose_contract_pr", "c1/propose#3",
                    candidate_id=cid, mode="drift", rationale="x")  # fmt: skip
    assert err and "propose_contract_pr" in json.dumps(out)
    # The validator passes it; from another chain it is still unknown.
    call("validator", "validate_contract", "c1/validate", feed=feed, contract_yaml=_candidate(),
         file_sha256=sha)  # fmt: skip
    err, d = call("validator", "check_dbt_models", "c1/validate", candidate_id=cid)
    assert d["ok"] is True and d["generated"] == [], d  # eligibility's models are hand-written
    err, out = call("intake_orchestrator", "propose_contract_pr", "c2/propose#4",
                    candidate_id=cid, mode="drift", rationale="x")  # fmt: skip
    assert "no candidate" in json.dumps(out)
    # Not yet reviewed (S10.5): refused. The reviewer sees the packet (no vendor values)
    # and passes it; a verdict from any other member is not the reviewer's.
    err, out = call("intake_orchestrator", "propose_contract_pr", "c1/propose#5",
                    candidate_id=cid, mode="drift", rationale="x")  # fmt: skip
    assert "reviewer has not passed" in json.dumps(out)
    err, packet = call("reviewer", "get_review_packet", "c1/review", candidate_id=cid)
    assert packet["candidate_yaml"] and packet["profile"]["columns"] and "loosening" in packet
    err, out = call("contract_author", "report_review", "c1/map", candidate_id=cid,
                    verdict="pass", reasons=[])  # fmt: skip
    assert err  # not in the map worker's policy
    err, out = call("reviewer", "report_review", "c1/review", candidate_id=cid,
                    verdict="revise", reasons=[])  # fmt: skip
    assert "needs at least one reason" in json.dumps(out)
    err, out = call("reviewer", "report_review", "c1/review", candidate_id=cid,
                    verdict="pass", reasons=[])  # fmt: skip
    assert not err and out["verdict"] == "pass", out
    # Now it is proposed, with the map worker's rationale and the verdict, for a person.
    err, out = call("intake_orchestrator", "propose_contract_pr", "c1/propose#6",
                    candidate_id=cid, mode="drift", rationale="ignored")  # fmt: skip
    assert not err and out["status"] == "pending", out
    (payload,) = rows(warehouse, "select payload from ops.approvals")[0]
    assert payload["rationale"] == "GOLD is a new plan." and payload["chain_id"] == "c1"
    assert payload["intake_job_id"] == jid
    # Once the job has ended, nothing more is proposed for it.
    finish(warehouse.conn, jid, "proposed")
    err, out = call("intake_orchestrator", "propose_contract_pr", "c1/propose#7",
                    candidate_id=cid, mode="drift", rationale="x")  # fmt: skip
    assert "no running intake job" in json.dumps(out)


def test_labels_are_fixed_choices(world):
    call, job, _ = world
    err, out = call("profile", "report_profile", "c1/profile", job_id=job["job_id"],
                    columns=[{"column": "nope", "phi": True, "role": "code"}])  # fmt: skip
    assert "no column" in json.dumps(out)
    err, out = call("profile", "report_profile", "c1/profile", job_id=job["job_id"],
                    columns=[{"column": "plan_code", "phi": True, "role": "boss"}])  # fmt: skip
    assert err  # the schema refuses a role outside the list


def test_a_refused_candidate_goes_back_to_map_once_then_the_job_fails(warehouse, monkeypatch):
    """The validator's refusal sends the errors back to the map worker; a second refusal
    ends the job `failed`, with nothing proposed."""
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    from dip.agents import intake_graph
    from tests.integration.test_contract_author import Scripted, _gold_added, team

    real = intake_graph._call
    seen: list[str] = []

    async def refusing(client, tool, run_id, **args):
        out = await real(client, tool, run_id, **args)
        if tool == "validate_contract":
            seen.append(run_id)
            return {"valid": False, "errors": ["refused for the test"]}
        return out

    monkeypatch.setattr(intake_graph, "_call", refusing)
    maps = iter([Scripted(_gold_added), Scripted(_gold_added)])
    others = team(None)
    r = author_evals.run_case(CASE, DSN, lambda s: next(maps) if s == "map" else others(s))
    assert r.outcome == "failed" and "refused the candidate twice" in r.reason
    assert not r.proposed and len(seen) == 2
    assert rows(warehouse, "select count(*) from ops.approvals") == [(0,)]


def test_the_runner_claims_nothing_without_keys_and_records_each_outcome(warehouse):
    from dip.agents import intake_runner
    from dip.agents.llm import NoProvider

    with warehouse.conn.cursor() as cur:
        a = queue(cur, "finance.a", "onboard", "a" * 64, "landing")
        b = queue(cur, "finance.b", "onboard", "b" * 64, "landing")
    warehouse.conn.commit()
    keys = {v: "k" for v in ("DIP_MCP_URL", *intake_runner.KEYS.values())}
    assert intake_runner.run(warehouse.conn, env=keys)["claimed"] == 0  # no model key
    assert intake_runner.run(warehouse.conn, env={"ANTHROPIC_API_KEY": "x"})["claimed"] == 0
    env = {**keys, "ANTHROPIC_API_KEY": "x"}

    def runner(conn, job, env_, providers):
        if job["job_id"] == a:
            return {"outcome": "proposed", "approval_id": 9, "chain_id": "intake-a",
                    "providers": {"profile": "anthropic", "map": "gemini"}}  # fmt: skip
        exc = NoProvider("anthropic: 529; gemini: 503")
        exc.step = "profile"
        raise exc

    s = intake_runner.run(warehouse.conn, env=env, limit=3, runner=runner)
    assert s["done"] == {a: "proposed", b: "queued"}  # b waits for a provider; the run stops
    got = rows(warehouse, "select job_id, status, approval_id, provider from ops.intake_job"
                          " order by job_id")  # fmt: skip
    assert got == [(a, "proposed", 9, "profile:anthropic map:gemini"), (b, "queued", None, None)]


def test_a_job_without_a_chain_serves_no_profile(world, warehouse):
    call, job, _ = world
    call("profile", "report_profile", "c1/profile", job_id=job["job_id"], columns=[])
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.intake_job set chain_id = null")
    warehouse.conn.commit()
    err, out = call("contract_author", "get_intake_profile", "c1/map", job_id=job["job_id"])
    assert "another chain" in json.dumps(out)


def test_requeue_and_a_new_run_leave_nothing_of_the_last_claim(warehouse):
    from dip.agents import intake_runner

    with warehouse.conn.cursor() as cur:
        a = queue(cur, "finance.a", "onboard", "a" * 64, "landing")
    warehouse.conn.commit()
    claim_one(warehouse.conn, "run-1")
    assert not intake_runner.requeue(warehouse.conn, a, "outage", "run-0")  # not its claim
    assert intake_runner.requeue(warehouse.conn, a, "outage", "run-1")
    with warehouse.conn.cursor() as cur:
        cur.execute("update ops.intake_job set profile = '{\"old\": true}'")
    warehouse.conn.commit()
    job = claim_one(warehouse.conn, "run-2")
    seen = {}

    def fake_run_intake(team, job_, chain_id):
        seen["profile"] = rows(warehouse, "select profile from ops.intake_job")[0][0]

        async def done():
            return {"outcome": "failed", "reason": "test"}

        return done()

    import dip.agents.intake_graph as ig

    real, ig.run_intake = ig.run_intake, fake_run_intake
    try:
        intake_runner.run_job(warehouse.conn, job, {"DIP_MCP_URL": "u", **{
            v: "k" for v in intake_runner.KEYS.values()}}, ("anthropic",))  # fmt: skip
    finally:
        ig.run_intake = real
    assert seen["profile"] is None  # the earlier run's profile is gone
