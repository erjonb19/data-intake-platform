"""The model behind the agents, and recordings so CI never calls it.

`AnthropicLLM` calls the Messages API with the model and settings in config/llm.toml
(`claude-haiku-5-5` unless DIP_LLM_MODEL overrides it; the key comes from
ANTHROPIC_API_KEY). Every response is reduced to a plain dict (`Message`), the same shape
a recording stores.

`Recording` replays a run's responses in order. A run is recorded once, live, with
`Recorder` (python -m dip triage eval --record); CI then replays it: the agent loop, the
MCP server, the policy and the scoring all run for real, only the model's turns come from
the file. Each recorded turn keeps a fingerprint of what the model was sent (the system
prompt, the tools, and the latest message: the task or the tool results, with dates
masked); replay fails if the agent now sends anything else (a changed prompt, tool or tool
output), or asks for more turns than were recorded, instead of replaying stale answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


_DATES = re.compile(r"\d{4}-\d{2}-\d{2}(T[0-9:.+-]+Z?)?")


def fingerprint(system: str, tools: list[dict], messages: list[dict]) -> str:
    """What the model was sent this turn, minus dates (tool results show the day a file was
    quarantined, which is the day the eval ran)."""
    doc = json.dumps({"system": system, "tools": tools, "last": messages[-1]},
                     sort_keys=True, default=str)  # fmt: skip
    return hashlib.sha256(_DATES.sub("DATE", doc).encode()).hexdigest()


class Recorder:
    """A live LLM whose responses are kept, to be written as a recording."""

    def __init__(self, inner: LLM):
        self.inner, self.messages, self.fingerprints = inner, [], []

    def create(self, **kwargs: Any) -> Message:
        m = self.inner.create(**kwargs)
        self.messages.append(m)
        self.fingerprints.append(fingerprint(kwargs["system"], kwargs["tools"],
                                             kwargs["messages"]))  # fmt: skip
        return m

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = [{"content": m.content, "stop_reason": m.stop_reason, "usage": m.usage,
                "model": m.model, "request_sha256": fp}
               for m, fp in zip(self.messages, self.fingerprints, strict=True)]  # fmt: skip
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                        newline="\n")  # fmt: skip


class Recording:
    def __init__(self, path: Path):
        self.path = path
        doc = json.loads(path.read_text("utf-8"))
        self.fingerprints = [m.pop("request_sha256", None) for m in doc]
        self.messages = [Message(**m) for m in doc]
        self.turn = 0

    def create(self, **kwargs: Any) -> Message:
        if self.turn >= len(self.messages):
            raise RuntimeError(
                f"{self.path.name}: the agent asked for turn {self.turn + 1}, but only "
                f"{len(self.messages)} were recorded; re-record (dip triage eval --record)"
            )
        expected = self.fingerprints[self.turn]
        got = fingerprint(kwargs["system"], kwargs["tools"], kwargs["messages"])
        if expected is not None and expected != got:
            raise RuntimeError(
                f"{self.path.name}, turn {self.turn + 1}: the agent sent the model something "
                "other than what was recorded (prompt, tools or tool output changed); "
                "re-record (dip triage eval --record)"
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
