"""One chain's trace (Stage 11, S11.2; PLAN.md section 8.13): its model calls and tool calls
as spans in time order, from the `ops.trace_span` view, with totals per step.

    python -m dip trace CHAIN_ID

The approvals page shows the same (its audit tab, `/api/trace?chain=`). Ids, names, times,
status, tokens and cost only: the view reads nothing else.

Model spans are timed by the agent's process and tool spans by the database (each audit
row's insert time, `clock_timestamp()`). On one machine or one kind node they share a
clock; across hosts whose clocks differ, spans close together can show out of order.
"""

from __future__ import annotations

from typing import Any

COLUMNS = ("span_id", "kind", "step", "name", "actor", "run_id", "started_at", "finished_at",
           "duration_ms", "status", "input_tokens", "output_tokens", "cost_usd")  # fmt: skip


def spans(conn: Any, chain_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"select {', '.join(COLUMNS)} from ops.trace_span where chain_id = %s"
            # on a tie the model span comes first ('model' < 'tool'): the agent asks the
            # model before it calls the tool the model chose
            " order by started_at, kind, span_id",
            (chain_id,),
        )
        out = [dict(zip(COLUMNS, r, strict=True)) for r in cur.fetchall()]
    conn.commit()
    for s in out:
        if s["cost_usd"] is not None:
            s["cost_usd"] = float(s["cost_usd"])
    return out


def totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per step, in the order steps first appear: model calls, tool calls, tokens, cost, and
    the step's wall time (its first start to its last end)."""
    out: dict[str, dict[str, Any]] = {}
    for s in rows:
        t = out.setdefault(s["step"] or "-", {
            "step": s["step"] or "-", "model_calls": 0, "tool_calls": 0, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0, "started_at": s["started_at"],
            "finished_at": s["finished_at"]})  # fmt: skip
        t["model_calls" if s["kind"] == "model" else "tool_calls"] += 1
        t["input_tokens"] += s["input_tokens"] or 0
        t["output_tokens"] += s["output_tokens"] or 0
        t["cost_usd"] += s["cost_usd"] or 0.0
        t["started_at"] = min(t["started_at"], s["started_at"])
        t["finished_at"] = max(t["finished_at"], s["finished_at"])
    for t in out.values():
        t["wall_ms"] = int((t["finished_at"] - t["started_at"]).total_seconds() * 1000)
        t["cost_usd"] = round(t["cost_usd"], 6)
    return list(out.values())


def render(chain_id: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"no spans for chain {chain_id!r}"
    t0 = rows[0]["started_at"]
    lines = [f"chain {chain_id}: {len(rows)} spans",
             f"{'+ms':>8} {'ms':>6}  {'kind':5} {'step':10} {'name':28} {'status':14} "
             f"{'in':>7} {'out':>6} {'$':>9}"]  # fmt: skip
    for s in rows:
        offset = int((s["started_at"] - t0).total_seconds() * 1000)
        tokens = ("", "") if s["kind"] == "tool" else (s["input_tokens"], s["output_tokens"])
        cost = "" if s["cost_usd"] is None else f"{s['cost_usd']:.6f}"
        lines.append(f"{offset:>8} {s['duration_ms']:>6}  {s['kind']:5} {(s['step'] or '-'):10} "
                     f"{s['name'][:28]:28} {s['status'][:14]:14} {tokens[0]!s:>7} "
                     f"{tokens[1]!s:>6} {cost:>9}")  # fmt: skip
    lines.append("")
    for t in totals(rows):
        lines.append(f"{t['step']}: {t['model_calls']} model calls, {t['tool_calls']} tool calls,"
                     f" {t['input_tokens']} in / {t['output_tokens']} out tokens,"
                     f" ${t['cost_usd']:.6f}, {t['wall_ms']} ms")  # fmt: skip
    return "\n".join(lines)
