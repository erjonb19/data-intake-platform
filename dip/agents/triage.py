"""The failure-triage agent (Stage 4): open quarantine in, one proposal per group out, for a
person to approve. In Act mode (Stage 6, `act`) it runs one approval a person approved.

The agent is an MCP client: it connects to the platform's MCP server over Streamable HTTP
with its own API key, lists the tools, and lets the model call them through
`call_tool`. It holds no database connection and no policy of its own; the server decides
every call (agents/policies/triage.yaml) and audits it. The loop here only moves messages
between the model and the server, stops after `max_turns`, and records what happened so
the evals can score it.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dip.agents.llm import LLM, PROMPT_DIR, Message

PROMPT = (PROMPT_DIR / "triage.md").read_text(encoding="utf-8")
TASK = (
    "Triage the open quarantine now: look at every group, then propose one action per "
    "group. Finish with the one-line summary per group."
)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    is_error: bool
    result: str


@dataclass
class TriageRun:
    run_id: str
    calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    stop: str = ""
    summary: str = ""
    usage: list[dict[str, int]] = field(default_factory=list)
    model: str = ""

    @property
    def proposals(self) -> list[dict[str, Any]]:
        out = []
        for c in self.calls:
            if c.name == "propose_operation" and not c.is_error:
                out.append({**c.args, "approval_id": json.loads(c.result).get("approval_id")})
        return out

    @property
    def denied(self) -> list[ToolCall]:
        return [c for c in self.calls if c.is_error and c.result.startswith("denied by policy")]


@asynccontextmanager
async def mcp_client(url: str, api_key: str):
    """An MCP session to the platform's server, authenticated with one agent's key."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {api_key}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


async def run(client: Any, llm: LLM, max_turns: int = 30, run_id: str | None = None) -> TriageRun:
    """Run the agent against a connected MCP client (from `mcp_client`, or in-process)."""
    return await run_agent(client, llm, PROMPT, TASK, max_turns, run_id)


ACT_PROMPT = (PROMPT_DIR / "triage_act.md").read_text(encoding="utf-8")


def act_task(approval_id: int) -> str:
    return (
        f"A person approved approval {approval_id}. Run it with execute_approved_operation"
        " and report the result."
    )


async def act(client: Any, llm: LLM, approval_id: int, max_turns: int = 6,
              run_id: str | None = None) -> TriageRun:  # fmt: skip
    """Act mode (Stage 6): run one approval a person approved. The server decides: it runs
    only an approval this agent proposed, a person approved, unexpired and unchanged, with
    an action the policy lets the agent execute; any other call is refused and audited."""
    return await run_agent(client, llm, ACT_PROMPT, act_task(approval_id), max_turns, run_id)


async def run_agent(client: Any, llm: LLM, system: str, task: str, max_turns: int = 30,
                    run_id: str | None = None) -> TriageRun:  # fmt: skip
    """The tool loop every agent here shares: list the server's tools (the caller's policy
    decides which), then move messages between the model and the server until the model
    stops or `max_turns` is reached."""
    listed = await client.list_tools()
    tools = [
        {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in listed.tools
    ]
    out = TriageRun(run_id or uuid.uuid4().hex[:12])
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    while out.turns < max_turns:
        m: Message = llm.create(system=system, tools=tools, messages=messages)
        out.turns += 1
        out.usage.append(m.usage)
        out.model = m.model or out.model
        messages.append({"role": "assistant", "content": m.content})
        if m.stop_reason != "tool_use" or not m.tool_uses:
            out.stop, out.summary = m.stop_reason, m.text
            break
        results = []
        for use in m.tool_uses:
            args = dict(use.get("input") or {})
            r = await client.call_tool(use["name"], {**args, "_run_id": out.run_id})
            text = "\n".join(getattr(c, "text", "") for c in r.content)
            out.calls.append(ToolCall(use["name"], args, bool(r.is_error), text))
            results.append({"type": "tool_result", "tool_use_id": use["id"], "content": text,
                            "is_error": bool(r.is_error)})  # fmt: skip
        messages.append({"role": "user", "content": results})
    else:
        out.stop = "max_turns"
    return out
