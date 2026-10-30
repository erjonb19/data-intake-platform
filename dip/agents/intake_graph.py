"""The intake team (Stage 10, S10.4; PLAN.md section 8.12): an orchestrator in code and three
workers, as a LangGraph graph with no model routing it (section 4.1).

    profile ──▶ map ──▶ validate ──▶ propose ──▶ end
                 ▲          │
                 └──────────┘ once, with the validator's errors

  profile   a model (key `profile`): reads the job's file through diff_schema and labels
            each column (report_profile). The server builds the profile's facts itself.
  map       the contract author, a model (key `contract_author`): reads the profile, writes
            a candidate contract, validates it and submits it (submit_candidate), or
            declines (a vendor mistake, or a column no contract can declare).
  validate  code (key `validator`): validate_contract and check_dbt_models on the submitted
            candidate, recorded by the server under the validator's name. A failure goes
            back to map once with the errors; a second failure ends the job `failed`.
  propose   code (key `intake_orchestrator`): propose_contract_pr on that candidate. The
            server proposes only a candidate submitted and passed in this chain while its
            job runs; the result is an ops.approvals row for a person.

One chain id ties the steps' run ids together (`<chain>/profile`, `/map`, `/validate`,
`/propose`), so `ops.audit_log` shows the whole job. Each model step runs on its providers
in order (Stage 10, S10.1). Nothing is written but the profile, the candidates and, at the
end, a pending proposal.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from dip.agents import triage
from dip.agents.graph import run_step
from dip.agents.llm import LLM, PROMPT_DIR, Failover

PROFILE_PROMPT = (PROMPT_DIR / "profile.md").read_text(encoding="utf-8")
MAP_PROMPT = (PROMPT_DIR / "contract_author.md").read_text(encoding="utf-8")
STEPS = ("profile", "map")


class IntakeState(TypedDict, total=False):
    chain_id: str
    job: dict[str, Any]
    profile_run: dict[str, Any] | None
    map_run: dict[str, Any] | None
    map_runs: list[dict[str, Any]]
    candidate_id: str | None
    validation: dict[str, Any] | None
    errors: list[str]
    attempts: int
    approval_id: int | None
    outcome: str  # proposed | no_change | failed
    reason: str | None
    providers: dict[str, str]


@dataclass
class IntakeTeam:
    """What the graph's nodes use: a warehouse connection, an MCP client per member, and a
    model (or a Failover) per model step."""

    conn: Any
    client_for: Callable[[str], Any]  # "profile" | "map" | "validator" | "orchestrator"
    llm_for: Callable[[str], LLM | Failover]  # "profile" | "map"
    max_turns: int = 30
    on_step: Callable[[str, str], None] | None = None


def profile_task(job: dict[str, Any]) -> str:
    return (
        f"Intake job {job['job_id']}: the {job['mode']} of feed `{job['feed']}`. Profile "
        f"its file {job['file_sha256']}: label every column, then call report_profile "
        f"with job_id {job['job_id']}."
    )


def map_task(job: dict[str, Any], errors: list[str]) -> str:
    if job["mode"] == "onboard":
        what = (
            f"Onboard a new vendor feed, `{job['feed']}`. Write its first contract "
            "(version 1), validate it, and submit it."
        )
    else:
        what = (
            f"Feed `{job['feed']}`: its file no longer fits its contract. Find what "
            "changed, decide whether it is a deliberate change the contract should accept, "
            "and if so edit the contract, validate it, and submit it."
        )
    text = (
        f"Intake job {job['job_id']}. {what} The file's profile: get_intake_profile("
        f"{job['job_id']}); the file is {job['file_sha256']}."
    )
    if errors:
        text += (
            " The validator refused your last candidate: "
            + json.dumps(errors)[:3000]
            + " Fix it, validate it and submit it again."
        )
    return text


def _summary(run: triage.TriageRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "turns": run.turns,
        "stop": run.stop,
        "usage": run.usage,
        "model": run.model,
        "summary": run.summary[:2000],
        "calls": [
            {"name": c.name, "is_error": c.is_error, "args": c.args, "result": c.result[:4000]}
            for c in run.calls
        ],
        "denied": [f"{c.name}: {c.result[:200]}" for c in run.denied],
    }


async def _call(client: Any, tool: str, run_id: str, **args: Any) -> dict[str, Any]:
    r = await client.call_tool(tool, {**args, "_run_id": run_id})
    text = "\n".join(getattr(c, "text", "") for c in r.content)
    try:
        out = json.loads(text)
    except ValueError:
        out = {"error": text}
    if r.is_error and "error" not in out:
        out["error"] = text[:500]
    return out if isinstance(out, dict) else {"result": out}


def build_graph(team: IntakeTeam) -> Any:
    from langgraph.graph import END, START, StateGraph

    async def profile(state: IntakeState) -> dict[str, Any]:
        job = state["job"]
        run_id = f"{state['chain_id']}/profile"

        async def body(llm: LLM, attempt: str) -> triage.TriageRun:
            async with team.client_for("profile") as client:
                return await triage.run_agent(
                    client, llm, PROFILE_PROMPT, profile_task(job), team.max_turns, attempt
                )

        run, provider = await run_step(team, "profile", run_id, body)  # type: ignore[arg-type]
        with team.conn.cursor() as cur:
            cur.execute(
                "select profile is not null from ops.intake_job where job_id = %s", (job["job_id"],)
            )
            (has,) = cur.fetchone()
        team.conn.commit()
        out: dict[str, Any] = {
            "profile_run": _summary(run),
            "providers": {**state.get("providers", {}), "profile": provider},
        }
        if not has:
            out |= {"outcome": "failed", "reason": "the profile worker reported no profile"}
        return out

    async def map_(state: IntakeState) -> dict[str, Any]:
        job = state["job"]
        attempts = state.get("attempts", 0) + 1
        run_id = f"{state['chain_id']}/map" + (f"#{attempts}" if attempts > 1 else "")

        async def body(llm: LLM, attempt: str) -> triage.TriageRun:
            async with team.client_for("map") as client:
                return await triage.run_agent(
                    client,
                    llm,
                    MAP_PROMPT,
                    map_task(job, state.get("errors", [])),
                    team.max_turns,
                    attempt,
                )

        run, provider = await run_step(team, "map", run_id, body)  # type: ignore[arg-type]
        submitted = [c for c in run.calls if c.name == "submit_candidate" and not c.is_error]
        summary = _summary(run)
        out: dict[str, Any] = {
            "map_run": summary,
            "map_runs": [*state.get("map_runs", []), summary],
            "attempts": attempts,
            "errors": [],
            "providers": {**state.get("providers", {}), "map": provider},
        }
        if not submitted:
            return out | {
                "candidate_id": None,
                "outcome": "no_change",
                "reason": (run.summary or "the map worker submitted nothing")[:2000],
            }
        return out | {"candidate_id": submitted[-1].args["candidate_id"]}

    def after_map(state: IntakeState) -> str:
        return "validate" if state.get("candidate_id") and not state.get("outcome") else "end"

    async def validate(state: IntakeState) -> dict[str, Any]:
        job, cid = state["job"], state["candidate_id"]
        run_id = f"{state['chain_id']}/validate"
        with team.conn.cursor() as cur:
            cur.execute(
                "select contract_yaml from ops.contract_candidate where chain_id = %s"
                " and candidate_id = %s",
                (state["chain_id"], cid),
            )
            row = cur.fetchone()
        team.conn.commit()
        if row is None:
            return {"outcome": "failed", "reason": f"candidate {cid} is not in this chain"}
        async with team.client_for("validator") as client:
            checked = await _call(
                client,
                "validate_contract",
                run_id,
                feed=job["feed"],
                contract_yaml=row[0],
                file_sha256=job["file_sha256"],
            )
            dbt = await _call(client, "check_dbt_models", run_id, candidate_id=cid)
        errors = []
        if checked.get("error") or not checked.get("valid"):
            errors += checked.get("errors") or [checked.get("error", "not valid")]
        elif checked.get("dry_run", {}).get("file_rejected"):
            errors.append(f"it still rejects the file: {checked['dry_run']}")
        elif checked.get("candidate_id") != cid:
            errors.append("the validator's candidate differs from the one submitted")
        if not dbt.get("ok"):
            errors.append(f"dbt: {dbt.get('error') or dbt.get('dbt_output') or dbt}")
        out: dict[str, Any] = {"validation": {"contract": checked, "dbt": dbt}, "errors": errors}
        if errors and state.get("attempts", 1) >= 2:
            out |= {
                "outcome": "failed",
                "reason": "the validator refused the candidate twice: "
                + "; ".join(map(str, errors))[:1500],
            }
        return out

    def after_validate(state: IntakeState) -> str:
        if state.get("outcome"):
            return "end"
        return "map" if state.get("errors") else "propose"

    async def propose(state: IntakeState) -> dict[str, Any]:
        job = state["job"]
        async with team.client_for("orchestrator") as client:
            r = await _call(
                client,
                "propose_contract_pr",
                f"{state['chain_id']}/propose",
                candidate_id=state["candidate_id"],
                mode=job["mode"],
                rationale="Submitted by the map worker; passed by the validator.",
            )
        if r.get("error") or not r.get("approval_id"):
            return {"outcome": "failed", "reason": f"the proposal was refused: {r}"[:2000]}
        return {"outcome": "proposed", "approval_id": int(r["approval_id"])}

    g = StateGraph(IntakeState)
    g.add_node("profile", profile)
    g.add_node("map", map_)
    g.add_node("validate", validate)
    g.add_node("propose", propose)
    g.add_edge(START, "profile")
    g.add_conditional_edges(
        "profile", lambda s: "end" if s.get("outcome") else "map", {"map": "map", "end": END}
    )
    g.add_conditional_edges("map", after_map, {"validate": "validate", "end": END})
    g.add_conditional_edges(
        "validate", after_validate, {"map": "map", "propose": "propose", "end": END}
    )
    g.add_edge("propose", END)
    return g.compile()


async def run_intake(
    team: IntakeTeam, job: dict[str, Any], chain_id: str | None = None
) -> IntakeState:
    graph = build_graph(team)
    return await graph.ainvoke(
        {
            "chain_id": chain_id or f"intake-{uuid.uuid4().hex[:10]}",
            "job": job,
            "attempts": 0,
            "errors": [],
            "providers": {},
            "map_runs": [],
        }
    )
