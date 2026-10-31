"""Stage 11 (S11.1): recorded agent runs write one ops.model_call row per model turn,
under the run's chain id, through the warehouse sink the runners use; the table is
append-only and the MCP server's login cannot read it."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from dip.agents import author_evals, evals, model_calls
from dip.agents.llm import Recording
from tests.integration.conftest import DSN, rows, scalar


def recorded_rows() -> list[tuple]:
    import psycopg

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select chain_id, run_id, step, source, turn, status, input_tokens,"
                    " cost_usd from ops.model_call order by call_id")  # fmt: skip
        return cur.fetchall()


@pytest.fixture
def work():
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    path = Path(tempfile.mkdtemp(prefix="mc"))  # short: Windows paths stay under 260
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_a_recorded_triage_run_writes_one_row_per_turn(work):
    case = evals.load_cases()[0]
    with model_calls.to_warehouse(DSN):
        result = evals.run_case(case, DSN, Recording(evals.RECORDINGS / f"{case['name']}.json"),
                                work)  # fmt: skip
    got = recorded_rows()
    assert len(got) == result.turns
    assert [r[4] for r in got] == list(range(1, result.turns + 1))
    assert {(r[3], r[5]) for r in got} == {("recorded", "ok")}
    assert len({r[1] for r in got}) == 1
    assert sum(r[6] for r in got) > 0


def test_a_recorded_intake_case_writes_every_step_under_its_chain(work):
    name = "onboard_lab_results"
    runs: dict[str, int] = {}

    def llm_for(step: str) -> Recording:
        runs[step] = runs.get(step, 0) + 1
        key = step + (str(runs[step]) if runs[step] > 1 else "")
        return Recording(author_evals.RECORDINGS / f"{name}_{key}.json")

    case = {c["name"]: c for c in author_evals.load_cases()}[name]
    with model_calls.to_warehouse(DSN):
        r = author_evals.run_case(case, DSN, llm_for, work)
    assert r.passed
    got = recorded_rows()
    chains = {row[0] for row in got}
    assert chains == {f"eval-{name}"}  # the eval harness's chain id for the case
    assert {row[2] for row in got} == {"profile", "map", "review"}
    turns = sum(len(Recording(p).messages) for p in author_evals.RECORDINGS.glob(f"{name}_*.json"))
    assert len(got) == turns


def test_the_table_is_append_only_and_not_the_mcp_servers(warehouse):
    import psycopg

    r = {c: None for c in model_calls.COLUMNS}
    r.update(provider="anthropic", source="live", prompt_sha256="0" * 64, turn=1, status="ok",
             input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0,
             cost_usd=0.000001, latency_ms=5)  # fmt: skip
    r["started_at"] = r["finished_at"] = scalar(warehouse, "select now()")
    sink = model_calls.WarehouseSink(DSN)
    sink(r)
    sink.close()
    assert rows(warehouse, "select provider, cost_usd::float from ops.model_call") == [
        ("anthropic", pytest.approx(0.000001))]  # fmt: skip
    for sql in ("update ops.model_call set cost_usd = 0", "delete from ops.model_call",
                "truncate ops.model_call"):  # fmt: skip
        with (pytest.raises(psycopg.errors.RaiseException, match="append-only"),
              warehouse.conn.cursor() as cur):  # fmt: skip
            cur.execute(sql)
        warehouse.conn.rollback()
    if scalar(warehouse, "select count(*) from pg_roles where rolname = 'dip_mcp'"):
        assert not scalar(warehouse, "select has_table_privilege('dip_mcp', 'ops.model_call',"
                                     " 'select')")  # fmt: skip
    assert rows(warehouse, "select calls, cost_usd::float from ops.agent_run_cost") == [
        (1, pytest.approx(0.000001))]  # fmt: skip
