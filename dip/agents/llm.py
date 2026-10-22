"""The model behind the agents, and recordings so CI never calls it.

`AnthropicLLM` calls the Messages API with the model and settings in config/llm.toml
(`claude-haiku-5-5` unless DIP_LLM_MODEL overrides it; the key comes from
ANTHROPIC_API_KEY). Every response is reduced to a plain dict (`Message`), the same shape
a recording stores.

`Recording` replays a run's responses in order. A run is recorded once, live, with
`Recorder` (python -m dip triage eval --record); CI then replays it: the agent loop, the
MCP server, the policy and the scoring all run for real, only the model's turns come from
the file. If the agent asks for more turns than were recorded the replay fails loudly
instead of guessing.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dip.contracts import REPO_ROOT

CONFIG = REPO_ROOT / "config" / "llm.toml"
# claude-haiku-5-5, first-party prices per million tokens (input, output), for run reports.
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


@dataclass
class Message:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text")


class LLM(Protocol):
    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Message: ...


def settings() -> dict[str, Any]:
    with CONFIG.open("rb") as f:
        primary = tomllib.load(f)["primary"]
    return {
        "model": os.environ.get("DIP_LLM_MODEL", primary["model"]),
        "effort": primary.get("effort", "medium"),
        "api_key_env": primary.get("api_key_env", "ANTHROPIC_API_KEY"),
    }


class AnthropicLLM:
    def __init__(self, max_tokens: int = 8000):
        import anthropic

        cfg = settings()
        self.model, self.effort, self.max_tokens = cfg["model"], cfg["effort"], max_tokens
        self.client = anthropic.Anthropic(api_key=os.environ.get(cfg["api_key_env"]))

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Message:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            # The system prompt and tool list are the same every turn: cache them.
            cache_control={"type": "ephemeral"},
        )
        d = response.to_dict()
        return Message(d["content"], d["stop_reason"], d.get("usage", {}), d.get("model", ""))


class Recorder:
    """A live LLM whose responses are kept, to be written as a recording."""

    def __init__(self, inner: LLM):
        self.inner, self.messages = inner, []

    def create(self, **kwargs: Any) -> Message:
        m = self.inner.create(**kwargs)
        self.messages.append(m)
        return m

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = [{"content": m.content, "stop_reason": m.stop_reason, "usage": m.usage,
                "model": m.model} for m in self.messages]  # fmt: skip
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


class Recording:
    def __init__(self, path: Path):
        self.path = path
        self.messages = [Message(**m) for m in json.loads(path.read_text("utf-8"))]
        self.turn = 0

    def create(self, **kwargs: Any) -> Message:
        if self.turn >= len(self.messages):
            raise RuntimeError(
                f"{self.path.name}: the agent asked for turn {self.turn + 1}, but only "
                f"{len(self.messages)} were recorded; re-record (dip triage eval --record)"
            )
        m = self.messages[self.turn]
        self.turn += 1
        return m


def cost_usd(usage_list: list[dict[str, int]], model: str) -> float:
    pin, pout = PRICES.get(model, (0.10, 0.50))
    tin = sum(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
              + u.get("cache_creation_input_tokens", 0) for u in usage_list)  # fmt: skip
    tout = sum(u.get("output_tokens", 0) for u in usage_list)
    return round(tin * pin / 1e6 + tout * pout / 1e6, 6)
