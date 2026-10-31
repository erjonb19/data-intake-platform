"""Per-call model records (Stage 11, S11.1; PLAN.md section 8.13).

Every model request an agent makes goes through `dip.agents.triage.run_agent`, which
reports it here: one `ops.model_call` row with the chain and run ids, the step, provider,
model, a SHA-256 of the system prompt, token counts, cost (`dip.agents.llm.cost_usd`, the
meter the spend caps use), timing, and how the call ended. Never the prompt or the
response text.

Where the rows go is set by whoever runs the agents, not by the LLM classes: a sink in a
context variable (`SINK`), set with `to_warehouse(dsn)` by the runners and the CLI. With no
sink set, nothing is recorded. The sink has its own autocommit connection, so a record
never commits or rolls back the agent's own transaction, and a record that cannot be
written is reported on stderr and never fails the agent's run.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from dip.agents.registry import chain_of

COLUMNS = (
    "chain_id", "run_id", "step", "provider", "source", "model", "prompt_sha256", "turn",
    "status", "stop_reason", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "cost_usd", "started_at", "finished_at", "latency_ms",
)  # fmt: skip

SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "dip_model_call_sink", default=None
)

PROVIDERS = {"AnthropicLLM": "anthropic", "GeminiLLM": "gemini"}


def step_of(run_id: str | None) -> str | None:
    """`chain-x/triage@gemini` and `intake-y/map#2` are steps `triage` and `map`; a run id
    with no chain (a standalone `dip triage run`) has none, as `chain_of` has no chain."""
    if not run_id or "/" not in run_id:
        return None
    return run_id.rpartition("/")[2].partition("@")[0].partition("#")[0] or None


def source_and_provider(llm: Any, model: str | None) -> tuple[str, str]:
    """`recorded` for a replayed recording, else `live`; the provider from the client's
    class, or for a recording from the model id it recorded."""
    from dip.agents.llm import Recorder, Recording

    source = "recorded" if isinstance(llm, Recording) else "live"
    inner = llm.inner if isinstance(llm, Recorder) else llm
    provider = PROVIDERS.get(type(inner).__name__)
    if provider is None:
        m = (model or "").lower()
        provider = "gemini" if "gemini" in m else "anthropic" if "claude" in m else (
            "unknown" if source == "recorded" else type(inner).__name__.lower())  # fmt: skip
    return source, provider


class Timer:
    """Wall-clock start and end (stored) and a monotonic duration (latency)."""

    def __init__(self) -> None:
        self.started, self._t0 = datetime.now(UTC), time.monotonic()

    def stop(self) -> tuple[datetime, datetime, int]:
        return self.started, datetime.now(UTC), int((time.monotonic() - self._t0) * 1000)


def row(llm: Any, run_id: str | None, system: str, turn: int, timer: Timer,
        message: Any = None, error: BaseException | None = None) -> dict[str, Any]:  # fmt: skip
    from dip.agents.llm import cost_usd

    started, finished, ms = timer.stop()
    usage = dict(getattr(message, "usage", None) or {})
    model = (getattr(message, "model", None) or getattr(llm, "model", None)
             or getattr(getattr(llm, "inner", None), "model", None)
             or next((m.model for m in getattr(llm, "messages", []) if m.model), None))  # fmt: skip
    source, provider = source_and_provider(llm, model)
    return {
        "chain_id": chain_of(run_id),
        "run_id": run_id,
        "step": step_of(run_id),
        "provider": provider,
        "source": source,
        "model": model,
        "prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
        "turn": turn,
        "status": "ok" if error is None else f"error: {type(error).__name__}",
        "stop_reason": getattr(message, "stop_reason", None),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "cost_usd": cost_usd([usage], model or "") if usage else 0.0,
        "started_at": started,
        "finished_at": finished,
        "latency_ms": ms,
    }


def record(r: dict[str, Any]) -> None:
    sink = SINK.get()
    if sink is None:
        return
    try:
        sink(r)
    except Exception as exc:  # noqa: BLE001 - a lost record never fails the agent's run
        print(f"model_calls: could not record a call ({type(exc).__name__}: {exc})",
              file=sys.stderr)  # fmt: skip


PLACEHOLDERS = ", ".join(["%s"] * len(COLUMNS))
INSERT = f"insert into ops.model_call ({', '.join(COLUMNS)}) values ({PLACEHOLDERS})"


class WarehouseSink:
    """Writes each row on its own autocommit connection, opened at the first row (with a
    5-second connect timeout, so an unreachable warehouse cannot stall the agent for long)
    and opened again after an error. After 3 failures in a row it stops trying."""

    GIVE_UP_AFTER = 3

    def __init__(self, dsn: str):
        self.dsn, self.conn, self.failures = dsn, None, 0

    def __call__(self, r: dict[str, Any]) -> None:
        if self.failures >= self.GIVE_UP_AFTER:
            return
        try:
            if self.conn is None:
                import psycopg

                self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            with self.conn.cursor() as cur:
                cur.execute(INSERT, tuple(r[c] for c in COLUMNS))
            self.failures = 0
        except Exception:
            self.failures += 1
            self.close()
            raise

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


@contextmanager
def to_warehouse(dsn: str | None) -> Iterator[None]:
    """Record every model call made inside the block to ops.model_call at `dsn` (no-op
    without one)."""
    if not dsn:
        yield
        return
    sink = WarehouseSink(dsn)
    token = SINK.set(sink)
    try:
        yield
    finally:
        SINK.reset(token)
        sink.close()
