"""Stage 11 (S11.3): every proposal carries the version of the run that made it, and each
run's steps record theirs: replayed recordings through the real MCP server."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from dip.agents import author_evals, chain_job, evals, model_calls
from dip.agents.llm import PROMPT_DIR, Recording
from dip.intake_jobs import queue
from tests.integration.conftest import DSN, rows


def payloads(action: str) -> list[dict]:
    import psycopg

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select payload from ops.approvals where action = %s order by approval_id",
                    (action,))  # fmt: skip
        return [r[0] for r in cur.fetchall()]


def sha(name: str) -> str:
    return model_calls.prompt_sha256((PROMPT_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def work():
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    path = Path(tempfile.mkdtemp(prefix="vs"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_a_triage_proposal_carries_its_runs_version(work):
    case = evals.load_cases()[0]
    evals.run_case(case, DSN, Recording(evals.RECORDINGS / f"{case['name']}.json"), work)
    got = [p for p in payloads("request_resend") + payloads("discard") + payloads("replay")
           + payloads("propose_contract_change") if "agent_version" in p]  # fmt: skip
    assert got, "the recorded triage run made no proposal"
    for p in got:
        v = p["agent_version"]
        assert v["agent"] == "triage" and v["model"].startswith("claude")
        assert v["prompt_sha256"] == sha("triage.md")


def test_the_intake_orchestrators_proposal_carries_every_steps_version(work):
    name = "onboard_lab_results"
    runs: dict[str, int] = {}

    def llm_for(step: str) -> Recording:
        runs[step] = runs.get(step, 0) + 1
        key = step + (str(runs[step]) if runs[step] > 1 else "")
        return Recording(author_evals.RECORDINGS / f"{name}_{key}.json")

    case = {c["name"]: c for c in author_evals.load_cases()}[name]
    assert author_evals.run_case(case, DSN, llm_for, work).passed
    [p] = payloads("contract_pr")
    v = p["agent_version"]
    assert v["agent"] == "intake_orchestrator"
    assert v["prompt_sha256"] == sha("contract_author.md")  # the map worker wrote it
    assert set(v["steps"]) == {"profile", "map", "review"}
    assert v["steps"]["profile"]["prompt_sha256"] == sha("profile.md")
    assert v["steps"]["review"]["prompt_sha256"] == sha("reviewer.md")
    assert {s["provider"] for s in v["steps"].values()} == {"recording"}  # replayed


def test_runs_record_each_steps_version(warehouse):
    from dip.agents import intake_runner

    versions = {"incident": {"provider": "anthropic", "model": "m", "prompt_sha256": "h"}}
    with warehouse.conn.cursor() as cur:
        jid = queue(cur, "finance.a", "onboard", "a" * 64, "landing")
    warehouse.conn.commit()
    keys = {v: "k" for v in ("DIP_MCP_URL", *intake_runner.KEYS.values())}
    intake_runner.run(warehouse.conn, env={**keys, "ANTHROPIC_API_KEY": "x"},
                      runner=lambda *a: {"outcome": "no_change", "chain_id": "intake-a",
                                         "versions": {"map": versions["incident"]}})  # fmt: skip
    assert rows(warehouse, "select versions from ops.intake_job where job_id = %s", (jid,)) == [
        ({"map": versions["incident"]},)]  # fmt: skip
    from tests.integration.test_agent_chain_job import MCP, _open

    iid = _open(warehouse, "healthcare.eligibility")
    chain_job.run(warehouse.conn, env={**MCP, "GEMINI_API_KEY": "g"},
                  runner=lambda *a: {"chain_id": "chain-v", "diagnosis": {"root_cause": "x"},
                                     "versions": versions})  # fmt: skip
    assert rows(warehouse, "select versions from ops.agent_run where incident_id = %s",
                (iid,)) == [(versions,)]  # fmt: skip
