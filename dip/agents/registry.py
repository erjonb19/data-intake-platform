"""The tool registry: every tool an agent (or a person, through the CLI) can call, with its
tier, and the per-principal policy that decides each call.

Tiers (PLAN.md section 7): `read` has no side effects; `propose` writes a request a person
must approve (an ops.approvals row); `act` changes data, and only for an approval a person
has already approved. A policy (agents/policies/<agent>.yaml) lists the tools an agent may
call, its highest tier, and limits (calls per run, the actions it may propose).

Enforcement is here, on the server side of MCP, so model output can never grant itself
more: an unknown tool, a tool outside the policy, a tier above the policy's, or a call over
a limit is refused. A tool may also refuse a call after looking at what it names (the Act
tool refuses an approval no person approved): it raises `Denied`, and the call is audited
as denied, with the reason, exactly like a policy refusal. Every call, allowed or refused,
appends a row to ops.audit_log with the principal, the tool, its tier, a SHA-256 of the
arguments (not the arguments: they can hold vendor data), the decision and its reason, the
result status and the latency.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dip.contracts import REPO_ROOT

# DIP_POLICY_DIR: where an installed package (the MCP image) finds them.
POLICY_DIR = Path(os.environ.get("DIP_POLICY_DIR", REPO_ROOT / "agents" / "policies"))
TIERS = ("read", "propose", "act")


# The policy of the call in progress, for tools whose output depends on it (PHI masking).
CURRENT_POLICY: ContextVar[Policy | None] = ContextVar("dip_current_policy", default=None)


def phi_masked() -> bool:
    policy = CURRENT_POLICY.get()
    return bool(policy and policy.mask_phi)


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
    # Hide the values of contract columns marked `phi` in what the tools return (the
    # reader: any person's MCP client). The triage agent sees records as received.
    mask_phi: bool = False

    @classmethod
    def load(cls, agent: str, root: Path = POLICY_DIR) -> Policy:
        spec = yaml.safe_load((root / f"{agent}.yaml").read_text(encoding="utf-8"))
        if spec.get("agent") != agent or spec.get("max_tier") not in TIERS:
            raise ValueError(f"policy {agent}: bad 'agent' or 'max_tier'")
        return cls(
            agent,
            spec["max_tier"],
            frozenset(spec["tools"]),
            spec.get("limits", {}),
            bool(spec.get("mask_phi", False)),
        )


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
        # Per principal over the last hour, whatever run id the client sends: the run id
        # comes from the client, so per-run limits stop a runaway loop but not a client
        # that lies about its run; these do.
        self._recent: dict[tuple[str, str], deque[float]] = {}
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
        for scope, limit in (("*", policy.limits.get("max_calls_per_hour")),
                             (name, tool_limits.get("max_per_hour"))):  # fmt: skip
            if limit is not None and len(self._window(policy.agent, scope)) >= limit:
                return f"over {limit} {'' if scope == '*' else name + ' '}calls in the last hour"
        return None

    def _window(self, principal: str, scope: str) -> deque[float]:
        q = self._recent.setdefault((principal, scope), deque())
        while q and q[0] < time.monotonic() - 3600:
            q.popleft()
        return q

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
        now = time.monotonic()
        self._window(policy.agent, "*").append(now)
        self._window(policy.agent, name).append(now)
        started = time.monotonic()
        status, decision, reason = "ok", "allowed", None
        try:
            token = CURRENT_POLICY.set(policy)
            try:
                result = self.tools[name].fn(principal=policy.agent, run_id=run_id, **args)
            finally:
                CURRENT_POLICY.reset(token)
            if isinstance(result, dict) and "error" in result:
                status = "error: tool"  # the tool refused the arguments
            return result
        except Denied as exc:  # the tool's own refusal: nothing ran
            self.conn.rollback()
            status, decision, reason = None, "denied", str(exc)
            raise
        except Exception as exc:
            self.conn.rollback()
            status = f"error: {type(exc).__name__}"
            raise
        finally:
            ms = int((time.monotonic() - started) * 1000)
            self.audit(policy.agent, run_id, name, tier, args, decision, reason, status, ms)
