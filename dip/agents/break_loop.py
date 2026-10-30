"""The break-and-fix loop (Stage 4, S4.5): every `dip break` scenario through detect ->
triage -> propose -> approve -> fix -> check, with the platform's real parts.

For each scenario in agents/evals/break_loop/scenarios.yaml:

1. **Break and detect.** A good file is broken (dip/breakages.py) and lands through the
   real intake into a fresh warehouse (the database must be a throwaway one). The intake
   must quarantine something.
2. **Triage and propose.** The triage agent works over the real MCP server on HTTP with
   its own key and policy, and proposes one action per group (a live model, or a
   recording of one: CI replays recordings and calls no model).
3. **Approve.** A person approves each proposal whose action is one the scenario accepts
   (`dip approvals approve`), and executes it (`dip approvals execute`, audited).
4. **Fix.** What the action leads to:
   - `request_resend`: the vendor's corrected file (the good file) arrives and loads; the
     open quarantine the request covered closes as `superseded`;
   - `propose_contract_change`: the contract change a person merges (the scenario's
     `fix`, a new contract version), then a second triage run, which must propose
     `replay`; a person approves and executes it, and the file is processed again;
   - `discard`: the entries close as discarded.
5. **Check.** No quarantine of the feed is left open, and the file (or its resend) loaded.

A scenario fails on any surprise: nothing quarantined, a group with no proposal or two, an
action the scenario does not accept, a refused tool call, a fix that does not load.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from dip import BUCKET_LANDING, sla
from dip.agents import approvals, evals, triage
from dip.agents.llm import LLM, cost_usd
from dip.agents.registry import human_policy
from dip.breakages import apply
from dip.contracts import REPO_ROOT, Contract, load_contracts
from dip.intake import Outcome, process_landing
from dip.storage import LocalStore
from dip.warehouse import Warehouse

ROOT = REPO_ROOT / "agents" / "evals" / "break_loop"
SCENARIOS = ROOT / "scenarios.yaml"
RECORDINGS = ROOT / "recordings"
CONTRACTS = ROOT / "contracts"  # pinned, as the triage evals pin theirs
BASE = REPO_ROOT / "tests" / "fixtures" / "break_loop"
AT = evals.INGEST_AT
PERSON = human_policy("loop@dip")


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]


@dataclass
class LoopResult:
    scenario: str
    steps: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    turns: int = 0
    triage_runs: int = 0
    usage: list[dict[str, int]] = field(default_factory=list)
    model: str = ""

    @property
    def passed(self) -> bool:
        return not self.problems


def _fixed_contract(contract: Contract, fix: dict[str, Any]) -> dict[str, Any]:
    """The contract as the merged change leaves it: a new version with the scenario's fix."""
    spec = json.loads(json.dumps(contract.spec))
    spec["version"] = contract.version + 1
    cols = spec["columns"]
    if r := fix.get("rename"):
        next(c for c in cols if c["name"] == r["from"])["name"] = r["to"]
    if a := fix.get("add_column"):
        cols.append(
            {
                "name": a["name"],
                "type": "string",
                "description": "Added after the vendor started sending it",
            }
        )
    if a := fix.get("allow"):
        next(c for c in cols if c["name"] == a["column"])["allowed"].append(a["value"])
    if d := fix.get("delimiter"):
        spec["format"]["delimiter"] = d
    return spec


def _triage(
    conn: Any, store: LocalStore, contracts: dict[str, Contract], llm: LLM, run_id: str
) -> triage.TriageRun:
    with evals.LocalServer(conn, store, contracts=contracts) as srv:

        async def go() -> triage.TriageRun:
            async with triage.mcp_client(srv.url, srv.key) as client:
                return await triage.run(client, llm, run_id=run_id)

        return asyncio.run(go())


def _still_open(conn: Any, ids: list[int]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from ops.quarantine where quarantine_id = any(%s) and status = 'open'",
            (ids,),
        )
        (n,) = cur.fetchone()
    conn.commit()
    return n > 0


def _review(
    conn: Any,
    store: LocalStore,
    contracts: dict[str, Contract],
    run: triage.TriageRun,
    groups: dict[str, list[int]],
    accept: list[str],
    out: LoopResult,
) -> list[str]:
    """Score one triage run, then approve and execute its proposals as a person; returns
    the actions executed."""
    out.turns += run.turns
    out.triage_runs += 1
    out.usage += run.usage
    out.model = run.model or out.model
    out.problems += [f"attempted {c.name} (denied)" for c in run.denied]
    out.problems += [
        f"refused proposal: {c.result[:100]}"
        for c in run.calls
        if c.name == "propose_operation" and c.is_error
    ]
    by_group: dict[str, list[dict]] = {}
    for p in run.proposals:
        by_group.setdefault(p["group_id"], []).append(p)
    for group, ids in groups.items():
        ps = by_group.get(group, [])
        if len(ps) != 1:
            out.problems.append(f"{group}: {len(ps)} proposals, expected 1")
            continue
        if ps[0]["action"] not in accept:
            out.problems.append(f"{group}: proposed {ps[0]['action']}, expected one of {accept}")
        if sorted(ps[0]["quarantine_ids"]) != sorted(ids):
            out.problems.append(f"{group}: proposal ids {ps[0]['quarantine_ids']} != open {ids}")
    out.problems += [
        f"{g}: proposal for a group that is not open" for g in by_group if g not in groups
    ]
    registry = evals_registry(conn, store, contracts)
    executed = []
    for p in run.proposals:
        aid = p["approval_id"]
        if p["action"] not in accept:
            approvals.decide(conn, aid, False, PERSON.agent, "not an action this case accepts")
            out.steps.append(f"rejected approval {aid} ({p['action']})")
            continue
        if not _still_open(conn, p["quarantine_ids"]):
            # An earlier approval already closed these entries (a replay releases every
            # entry of its file): nothing is left for this one to do.
            approvals.decide(conn, aid, False, PERSON.agent, "already resolved")
            out.steps.append(f"rejected approval {aid}: already resolved")
            continue
        approvals.decide(conn, aid, True, PERSON.agent, "break loop")
        try:
            result = registry.call(PERSON, None, "execute_approved_operation", {"approval_id": aid})
        except approvals.ApprovalError as exc:
            out.problems.append(f"approval {aid} ({p['action']}) failed to run: {exc}")
            continue
        executed.append(p["action"])
        out.steps.append(f"approved and executed {aid} {p['action']}: {json.dumps(result)[:120]}")
    return executed


def evals_registry(conn: Any, store: LocalStore, contracts: dict[str, Contract]) -> Any:
    from dip.agents.tools import build_registry

    return build_registry(conn, store, contracts)


def _land(
    conn: Any,
    store: LocalStore,
    contracts: dict[str, Contract],
    feed: str,
    name: str,
    data: bytes,
    at_hours: int,
) -> Outcome:
    store.put(BUCKET_LANDING, f"{feed}/{name}", data)
    [outcome] = process_landing(store, Warehouse(conn), contracts, AT + timedelta(hours=at_hours))
    return outcome


def _open(conn: Any, feed: str) -> dict[str, list[int]]:
    return {g: ids for g, ids in evals.open_groups(conn).items() if g.startswith(feed + "|")}


def run_scenario(sc: dict[str, Any], dsn: str, llm_for: Any, work: Path) -> LoopResult:
    """One scenario; `llm_for(name)` gives the model for one triage run (the recording
    `<scenario>.json`, then `<scenario>_after_fix.json`)."""
    import psycopg

    out = LoopResult(sc["name"])
    root = work / sc["name"]
    shutil.rmtree(root, ignore_errors=True)
    (root / "contracts").mkdir(parents=True)
    for path in CONTRACTS.glob("*.yaml"):
        shutil.copy(path, root / "contracts" / path.name)
    contracts = load_contracts((root / "contracts",))
    store = LocalStore(root / "store")
    feed = sc["feed"]
    good = (BASE / sc["base"]).read_bytes()
    with psycopg.connect(dsn) as conn:
        evals._reset(conn)
        if sc.get("timing") == "late":
            return _late(conn, store, contracts, sc, apply(good, sc["break"]), out)
        # 1. Break and detect.
        o = _land(conn, store, contracts, feed, sc["file"], apply(good, sc["break"]), 0)
        if sc["expect"] == "loads":  # a tolerated change: it must load as it is
            out.steps.append(f"detect: {o.status} {list(o.reason_codes)}")
            if o.status != "loaded" or _open(conn, feed):
                out.problems.append(f"expected it to load as it is: {o.status} {o.reason_codes}")
            elif _records(conn, o.file_sha256) != _good_records(good):
                out.problems.append("loaded, but its records differ from the good file's")
            out.steps.append("check: loaded, nothing quarantined" if not out.problems
                             else "check: FAIL")  # fmt: skip
            return out
        out.steps.append(f"detect: {o.status} {list(o.reason_codes)}")
        groups = _open(conn, feed)
        if not groups:
            out.problems.append(f"nothing quarantined: {o.status}")
            return out
        # 2-3. Triage, propose, approve, execute.
        run = _triage(conn, store, contracts, llm_for(sc["name"]), f"loop-{sc['name']}")
        out.actions = [p["action"] for p in run.proposals]
        executed = _review(conn, store, contracts, run, groups, sc["expect"], out)
        if out.problems:
            return out
        # 4. Fix.
        if "request_resend" in executed:
            resend = sc.get("resend") or _resend_name(sc["file"])
            r = _land(conn, store, contracts, feed, resend, good, 1)
            out.steps.append(
                f"fix: the vendor resent {resend}: {r.status}, {r.superseded} entries superseded"
            )
            if r.status != "loaded":
                out.problems.append(f"the resend did not load: {r.status} {r.reason_codes}")
        if "propose_contract_change" in executed:
            contract = contracts[feed]
            spec = _fixed_contract(contract, sc["fix"])
            contract.path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
            contracts = load_contracts((root / "contracts",))
            out.steps.append(f"fix: contract change merged ({feed} v{spec['version']})")
            groups = _open(conn, feed)
            run = _triage(
                conn,
                store,
                contracts,
                llm_for(f"{sc['name']}_after_fix"),
                f"loop-{sc['name']}-after-fix",
            )
            out.actions += [p["action"] for p in run.proposals]
            executed = _review(conn, store, contracts, run, groups, ["replay"], out)
            if out.problems:
                return out
            relanded = store.list(BUCKET_LANDING, f"{feed}/")
            outcomes = process_landing(store, Warehouse(conn), contracts, AT + timedelta(hours=2))
            out.steps.append(
                f"fix: replayed {[PurePosixPath(k).name for k in relanded]}: "
                f"{[x.status for x in outcomes]}"
            )
            if [x.status for x in outcomes] != ["loaded"]:
                out.problems.append(f"the replay did not load: {[asdict(x) for x in outcomes]}")
        # 5. Check.
        left = _open(conn, feed)
        if left:
            out.problems.append(f"still open after the fix: {sorted(left)}")
        out.steps.append("check: no open quarantine for the feed" if not left else "check: FAIL")
    return out


def _records(conn: Any, sha: str) -> list[dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute("select record from bronze.csv_record where file_sha256 = %s"
                    " order by row_number", (sha,))  # fmt: skip
        out = [r[0] for r in cur.fetchall()]
    conn.commit()
    return out


def _good_records(good: bytes) -> list[dict[str, str]]:
    """The good file's rows by column name: a reordered file must load the same values."""
    import csv
    import io

    return list(csv.DictReader(io.StringIO(good.decode("utf-8"))))


def _late(conn: Any, store: LocalStore, contracts: dict[str, Contract], sc: dict[str, Any],
          data: bytes, out: LoopResult) -> LoopResult:  # fmt: skip
    """A late file (S9.4): the SLA check opens FEED.LATE while nothing has arrived, the file
    then arrives and loads, and the next check closes the incident. No model: this is
    the SLA check's and the intake's job; the incident agent reads it (runbook 5)."""
    feed = sc["feed"]
    only = {feed: contracts[feed]}  # the scenario's feed alone, not every pinned contract

    def open_late() -> list[int]:
        with conn.cursor() as cur:
            cur.execute("select incident_id from ops.incident where feed = %s"
                        " and kind = 'FEED.LATE' and closed_at is null", (feed,))  # fmt: skip
            ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        return ids

    opened = sla.check(conn, only, AT)["opened"]
    out.steps.append(f"detect: nothing arrived; SLA check at {AT:%Y-%m-%d %H:%M} opened {opened}")
    if not open_late() or _open(conn, feed):
        out.problems.append("expected FEED.LATE open and nothing quarantined")
        return out
    o = _land(conn, store, contracts, feed, sc["file"], data, 1)
    closed = sla.check(conn, only, AT + timedelta(hours=1))["closed"]
    out.steps.append(f"fix: the file arrived an hour later: {o.status}; SLA check closed {closed}")
    if o.status != "loaded":
        out.problems.append(f"the late file did not load: {o.status} {o.reason_codes}")
    if open_late():
        out.problems.append("FEED.LATE still open after the file arrived")
    out.steps.append("check: loaded, incident closed" if not out.problems else "check: FAIL")
    return out


def _resend_name(name: str) -> str:
    p = PurePosixPath(name)
    return f"{p.stem}_resend{p.suffix}"


def report(results: list[LoopResult]) -> dict[str, Any]:
    model = next((r.model for r in results if r.model), "")
    usage = [u for r in results for u in r.usage]
    return {
        "summary": {
            "model": model,
            "scenarios": len(results),
            "passed": sum(r.passed for r in results),
            "triage_runs": sum(r.triage_runs for r in results),
            "turns": sum(r.turns for r in results),
            "cost_usd": cost_usd(usage, model),
        },
        "scenarios": [asdict(r) | {"passed": r.passed} for r in results],
    }
