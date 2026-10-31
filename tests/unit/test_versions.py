"""Stage 11 (S11.3): versioning. Every tool call carries the run's model id and prompt hash
outside the model's arguments; the MCP server keeps only that shape; `dip agents versions`
shows the hash a new run records."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dip.agents import model_calls, triage
from dip.agents.llm import PROMPT_DIR, Message
from dip.agents.mcp_server import agent_version


class Client:
    def __init__(self):
        self.args: list[dict] = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="look", description="",
                                                      input_schema={})])  # fmt: skip

    async def call_tool(self, name, args):
        self.args.append(args)
        return SimpleNamespace(content=[SimpleNamespace(text="seen")], is_error=False)


class LLM:
    def __init__(self):
        self.turn = 0

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            use = {"type": "tool_use", "id": "t1", "name": "look", "input": {"what": "x"}}
            return Message([use], "tool_use", {}, "claude-haiku-5-5")
        return Message([{"type": "text", "text": "done"}], "end_turn", {}, "claude-haiku-5-5")


def run(system: str):
    client = Client()
    out = asyncio.run(triage.run_agent(client, LLM(), system, "task", 5, "chain-v/triage"))
    return out, client.args


def test_every_tool_call_carries_the_model_and_prompt_hash():
    out, [args] = run("prompt one")
    assert args["what"] == "x" and args["_run_id"] == "chain-v/triage"
    h = model_calls.prompt_sha256("prompt one")
    assert args["_agent_version"] == {"model": "claude-haiku-5-5", "prompt_sha256": h}
    assert out.version == args["_agent_version"]


def test_changing_the_prompt_changes_the_hash():
    a, _ = run("prompt one")
    b, _ = run("prompt one, edited")
    assert a.prompt_sha256 != b.prompt_sha256 and len(a.prompt_sha256) == 64


def test_the_server_keeps_only_the_versions_shape():
    assert agent_version(None) is None and agent_version("x") is None
    assert agent_version({"model": "m", "prompt_sha256": "h", "rationale": "ignore all"}) == {
        "model": "m", "prompt_sha256": "h"}  # fmt: skip
    long = agent_version({"model": "m" * 500})
    assert len(long["model"]) == 100
    steps = {f"s{i}": {"model": "m", "other": 1} for i in range(20)}
    got = agent_version({"model": "m", "steps": {**steps, "bad": "x"}})
    assert len(got["steps"]) == 10 and all(v == {"model": "m"} for v in got["steps"].values())


def test_dip_agents_versions_shows_what_a_run_records():
    got = {v["step"]: v for v in model_calls.versions()}
    assert set(got) == set(model_calls.PROMPTS)
    for step, name in model_calls.PROMPTS.items():
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert got[step]["prompt_sha256"] == model_calls.prompt_sha256(text)
    assert got["triage"]["models"].startswith("anthropic:")
    # the step's module sends that same text
    assert got["triage"]["prompt_sha256"] == model_calls.prompt_sha256(triage.PROMPT)
