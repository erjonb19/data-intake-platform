"""The tool registry: every tool an agent (or a person, through the CLI) can call, with its
tier, and the per-principal policy that decides each call.

Tiers (PLAN.md section 7): `read` has no side effects; `propose` writes a request a person
must approve (an ops.approvals row); `act` changes data, and only for an approval a person
has already approved. A policy (agents/policies/<agent>.yaml) lists the tools an agent may
call, its highest tier, and limits (calls per run, the actions it may propose).

Enforcement is here, on the server side of MCP, so model output can never grant itself
more: an unknown tool, a tool outside the policy, a tier above the policy's, or a call over
a limit is refused. Every call, allowed or refused, appends a row to ops.audit_log with the
principal, the tool, its tier, a SHA-256 of the arguments (not the arguments: they can hold
vendor data), the decision and its reason, the result status and the latency.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dip.contracts import REPO_ROOT

POLICY_DIR = REPO_ROOT / "agents" / "policies"
TIERS = ("read", "propose", "act")


class Denied(PermissionError):
    """The policy refused the call; the refusal is in the audit log."""


@dataclass(frozen=True)
class Tool:
    name: str
    tier: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]


@dataclass
class Policy:
    agent: str
    max_tier: str
    tools: frozenset[str]
    limits: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, agent: str, root: Path = POLICY_DIR) -> Policy:
        spec = yaml.safe_load((root / f"{agent}.yaml").read_text(encoding="utf-8"))
        if spec.get("agent") != agent or spec.get("max_tier") not in TIERS:
            raise ValueError(f"policy {agent}: bad 'agent' or 'max_tier'")
        return cls(agent, spec["max_tier"], frozenset(spec["tools"]), spec.get("limits", {}))


# A person at the CLI: every tool, every tier. Their calls are audited like an agent's.
def human_policy(name: str) -> Policy:
    return Policy(f"human:{name}", "act", frozenset({"*"}))


def args_sha256(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


class Registry:
    def __init__(self, conn: Any):
        self.conn = conn
        self.tools: dict[str, Tool] = {}
        self._calls: Counter[tuple[str, str]] = Counter()  # (principal, run_id) -> calls
        self._per_tool: Counter[tuple[str, str, str]] = Counter()
        # One database connection: calls from concurrent MCP requests take turns.
        self._lock = threading.Lock()

    def register(self, tool: Tool) -> None:
        if tool.tier not in TIERS:
            raise ValueError(f"{tool.name}: unknown tier {tool.tier!r}")
        self.tools[tool.name] = tool

    def audit(self, principal: str, run_id: str | None, tool: str, tier: str, args: dict,
               decision: str, reason: str | None, result_status: str | None,
               latency_ms: int | None) -> None:  # fmt: skip
        with self.conn.cursor() as cur:
            cur.execute(
                "insert into ops.audit_log (principal, run_id, tool, tier, args_sha256,"
                " decision, reason, result_status, latency_ms)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    principal,
                    run_id,
                    tool,
                    tier,
                    args_sha256(args),
                    decision,
                    reason,
                    result_status,
                    latency_ms,
                ),  # fmt: skip
            )
        self.conn.commit()

    def check(self, policy: Policy, run_id: str | None, name: str, args: dict) -> str | None:
        """Why the policy refuses this call, or None if it allows it."""
        tool = self.tools.get(name)
        if tool is None:
            return f"unknown tool {name!r}"
        if "*" not in policy.tools and name not in policy.tools:
            return f"{name!r} is not in {policy.agent}'s policy"
        if TIERS.index(tool.tier) > TIERS.index(policy.max_tier):
            return f"{name!r} is tier {tool.tier}; {policy.agent} may use up to {policy.max_tier}"
        key = (policy.agent, run_id or "")
        if self._calls[key] >= policy.limits.get("max_calls_per_run", 10**9):
            return f"over {policy.limits['max_calls_per_run']} calls in this run"
        tool_limits = policy.limits.get(name, {})
        if self._per_tool[(*key, name)] >= tool_limits.get("max_per_run", 10**9):
            return f"over {tool_limits['max_per_run']} {name} calls in this run"
        if "actions" in tool_limits and args.get("action") not in tool_limits["actions"]:
            return f"action {args.get('action')!r} is not one {policy.agent} may propose"
        return None

    def call(self, policy: Policy, run_id: str | None, name: str, args: dict[str, Any]) -> Any:
        with self._lock:
            return self._call(policy, run_id, name, args)

    def _call(self, policy: Policy, run_id: str | None, name: str, args: dict[str, Any]) -> Any:
        tier = self.tools[name].tier if name in self.tools else "unknown"
        refusal = self.check(policy, run_id, name, args)
        key = (policy.agent, run_id or "")
        self._calls[key] += 1
        if refusal:
            self.audit(policy.agent, run_id, name, tier, args, "denied", refusal, None, None)
            raise Denied(refusal)
        self._per_tool[(*key, name)] += 1
        started = time.monotonic()
        status = "ok"
        try:
            return self.tools[name].fn(principal=policy.agent, run_id=run_id, **args)
        except Exception as exc:
            self.conn.rollback()
            status = f"error: {type(exc).__name__}"
            raise
        finally:
            ms = int((time.monotonic() - started) * 1000)
            self.audit(policy.agent, run_id, name, tier, args, "allowed", None, status, ms)
