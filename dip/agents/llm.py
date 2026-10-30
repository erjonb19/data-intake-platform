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

# DIP_LLM_CONFIG and DIP_PROMPT_DIR: where an installed package (the Airflow image's agent
# venv, Stage 9) finds them, like DIP_POLICY_DIR.
CONFIG = Path(os.environ.get("DIP_LLM_CONFIG", REPO_ROOT / "config" / "llm.toml"))
PROMPT_DIR = Path(os.environ.get("DIP_PROMPT_DIR", REPO_ROOT / "agents" / "prompts"))
# claude-haiku-5-5, first-party prices per million tokens (input, output), for run reports.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "gemini-3.1-flash-lite": (0.0, 0.0)}  # free tier


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
    # 16k output tokens per turn: a triage turn that files proposals for 15 groups at once
    # used 7,853 tokens live (2026-10-23), and two runs that hit the old 8k cap stopped
    # mid-turn with nothing filed. Under the SDK's limit for non-streaming requests.
    def __init__(self, max_tokens: int = 16000):
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


# --- The free fallback, per worker step (Stage 10, S10.1; PLAN.md section 8.12) ---------
#
# A step (one agent's whole run) starts on the first provider in config/llm.toml whose key
# is set. If that provider is unavailable (no key, a rate limit, an overload or a server
# error, a connection failure), the step starts again from its task on the next one. Never
# mid-conversation: the providers' histories (thinking blocks, Gemini's signed parts) do
# not translate safely. And never after the step has written anything (a propose- or
# act-tier call the server allowed), so a restart cannot write twice: that step fails.

FAILOVER_STATUS = {429, 500, 502, 503, 504, 529}


class StepWrote(RuntimeError):
    """The provider failed after the step had written: it is not restarted elsewhere."""


class NoProvider(RuntimeError):
    """Every provider was missing or unavailable, and the step wrote nothing."""

    step: str | None = None  # set by the graph: which step found no provider


def is_failover(exc: BaseException) -> bool:
    """Whether an error means "this provider is unavailable now", not a bug of ours: a
    429, an overload (529) or a 5xx, or no connection. A 400 (a malformed request) is ours
    and fails the step everywhere alike, so it is not retried on another provider.

    A step runs inside its MCP client's task group, which raises what failed inside it as
    an exception group: a group means "unavailable" only if everything in it does."""
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_failover(e) for e in exc.exceptions)
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status in FAILOVER_STATUS
    try:
        import anthropic

        if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
            return True
    except ImportError:  # pragma: no cover - the agents extra is not installed
        pass
    from dip.agents.gemini import GeminiError

    return isinstance(exc, GeminiError) and exc.status is None  # unreachable


def provider_names(env: dict[str, str] | None = None) -> list[str]:
    """The providers in config order (primary first) whose key is set in `env`. Only
    these are tried: AnthropicLLM with no key would fail at its first call with an error
    that is not a failover, so a provider without a key must never be in the list."""
    env = os.environ if env is None else env
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    out = []
    for p in [cfg["primary"], *cfg.get("fallback", [])]:
        if p["provider"] not in FACTORIES:
            raise ValueError(f"config/llm.toml names {p['provider']!r}, which has no client")
        if env.get(p.get("api_key_env", "")):
            out.append(p["provider"])
    return out


def _anthropic() -> LLM:
    return AnthropicLLM()


def _gemini() -> LLM:
    from dip.agents.gemini import GeminiLLM

    return GeminiLLM()


FACTORIES: dict[str, Any] = {"anthropic": _anthropic, "gemini": _gemini}


class Failover:
    """The providers to try for a step, in order. `run` runs the whole step on the first
    one and moves on only while the step has written nothing (`wrote(attempt)`).

    Each attempt is told its suffix: "" for the first, "@<provider>" for a restart. The
    graph adds it to the step's run id, so a restart starts with its own per-run call
    budget (the registry counts calls per run id) and its own audit trail."""

    def __init__(self, providers: list[tuple[str, Any]]):
        self.providers = providers  # (name, factory returning an LLM)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Failover:
        return cls([(n, FACTORIES[n]) for n in provider_names(env)])

    async def run(self, step: Any, wrote: Any) -> tuple[Any, str]:
        """`step(llm, suffix)` is awaited; returns (its result, the provider that answered).
        `wrote(suffix)` says whether that attempt wrote anything."""
        tried: list[str] = []
        attempts = 0
        for name, make in self.providers:
            try:
                llm = make()
            except LookupError as exc:  # no key for this one
                tried.append(f"{name}: {exc}")
                continue
            suffix = f"@{name}" if attempts else ""
            attempts += 1
            try:
                return await step(llm, suffix), name
            except Exception as exc:
                if not is_failover(exc):
                    raise
                if wrote(suffix):
                    raise StepWrote(
                        f"{name} failed after the step had written; not restarted on "
                        f"another provider: {type(exc).__name__}: {exc}"
                    ) from exc
                tried.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
        raise NoProvider("no provider answered: " + ("; ".join(tried) or "none configured"))
