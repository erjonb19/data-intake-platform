"""Per-call model records (Stage 11, S11.1; dip.agents.model_calls): one row per model
request through the shared agent loop, a failed request included, its cost from the same
meter as the caps, and never the prompt or the response text."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from dip.agents import model_calls, triage
from dip.agents.llm import Message, Recording, cost_usd

SECRET_PROMPT = "SYSTEM PROMPT TEXT 7f3a"
SECRET_ANSWER = "MODEL ANSWER TEXT 91bc"
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 4000,
         "cache_creation_input_tokens": 500}  # fmt: skip


class Client:
    """An MCP client with one tool that always answers."""

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="look", description="",
                                                      input_schema={})])  # fmt: skip

    async def call_tool(self, name, args):
        return SimpleNamespace(content=[SimpleNamespace(text="seen")], is_error=False)


class AnthropicLLM:  # named like the real client, so the provider is "anthropic"
    model = "claude-haiku-5-5"

    def __init__(self, fail_on: int | None = None):
        self.turn, self.fail_on = 0, fail_on

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == self.fail_on:
            raise TimeoutError("the provider did not answer")
        if self.turn == 1:
            return Message([{"type": "tool_use", "id": "t1", "name": "look", "input": {}}],
                           "tool_use", USAGE, "claude-haiku-5-5")  # fmt: skip
        return Message([{"type": "text", "text": SECRET_ANSWER}], "end_turn", USAGE,
                       "claude-haiku-5-5")  # fmt: skip


@pytest.fixture
def rows():
    got: list[dict] = []
    token = model_calls.SINK.set(got.append)
    yield got
    model_calls.SINK.reset(token)


def run(llm, run_id="chain-abc/triage"):
    return asyncio.run(triage.run_agent(Client(), llm, SECRET_PROMPT, "task", 5, run_id))


def test_one_row_per_request_with_its_ids_tokens_and_cost(rows):
    out = run(AnthropicLLM())
    assert out.turns == 2 and len(rows) == 2
    for i, r in enumerate(rows, 1):
        assert set(r) == set(model_calls.COLUMNS)
        assert (r["chain_id"], r["run_id"], r["step"], r["turn"]) == (
            "chain-abc", "chain-abc/triage", "triage", i)  # fmt: skip
        assert (r["provider"], r["source"], r["model"], r["status"]) == (
            "anthropic", "live", "claude-haiku-5-5", "ok")  # fmt: skip
        assert r["input_tokens"] == 1200 and r["cache_read_tokens"] == 4000
        assert r["cost_usd"] == cost_usd([USAGE], "claude-haiku-5-5")
        assert r["finished_at"] >= r["started_at"] and r["latency_ms"] >= 0
    assert [r["stop_reason"] for r in rows] == ["tool_use", "end_turn"]


def test_no_prompt_or_response_text_is_recorded(rows):
    run(AnthropicLLM())
    text = json.dumps(rows, default=str)
    assert SECRET_PROMPT not in text and SECRET_ANSWER not in text
    assert len(rows[0]["prompt_sha256"]) == 64


def test_a_failed_request_still_writes_its_row(rows):
    with pytest.raises(TimeoutError):
        run(AnthropicLLM(fail_on=2))
    assert [r["status"] for r in rows] == ["ok", "error: TimeoutError"]
    assert rows[1]["cost_usd"] == 0 and rows[1]["input_tokens"] == 0


def test_without_a_sink_nothing_is_recorded():
    assert model_calls.SINK.get() is None
    assert run(AnthropicLLM()).turns == 2  # and no error


def test_a_sink_that_fails_never_fails_the_run(capsys):
    def broken(row):
        raise ConnectionError("warehouse down")

    token = model_calls.SINK.set(broken)
    try:
        assert run(AnthropicLLM()).turns == 2
    finally:
        model_calls.SINK.reset(token)
    assert "could not record" in capsys.readouterr().err


def test_a_replayed_recording_is_marked_recorded(rows, tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps([{"content": [{"type": "text", "text": "x"}],
                                 "stop_reason": "end_turn", "usage": USAGE,
                                 "model": "gemini-3.1-flash-lite"}]), "utf-8")  # fmt: skip
    run(Recording(path), run_id="intake-1/map#2")
    [r] = rows
    assert (r["source"], r["provider"], r["step"], r["chain_id"]) == (
        "recorded", "gemini", "map", "intake-1")  # fmt: skip


@pytest.mark.parametrize("run_id, step", [
    ("chain-x/triage@gemini", "triage"), ("intake-y/map#2", "map"), ("abc123", None),
    (None, None),
])  # fmt: skip
def test_step_of(run_id, step):
    assert model_calls.step_of(run_id) == step


def test_a_spend_cap_refusal_sent_nothing_and_writes_no_row(rows):
    from dip.agents.llm import SpendCap

    class Capped(AnthropicLLM):
        def create(self, **kwargs):
            raise SpendCap("over the cap")

    with pytest.raises(SpendCap):
        run(Capped())
    assert rows == []


def test_the_warehouse_sink_stops_after_three_failures_in_a_row():
    sink = model_calls.WarehouseSink("postgresql://nobody@127.0.0.1:1/none")
    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017 - whatever the driver raises
            sink({c: None for c in model_calls.COLUMNS})
    assert sink.failures == 3 and sink.conn is None
    sink({c: None for c in model_calls.COLUMNS})  # given up: no attempt, no error
