"""Command line: `python -m dip <command>`.

  db-init                      create the bronze and ops tables
  land <feed> <files...>       copy local files into the landing bucket
  intake                       process everything in the landing bucket
  vocab-load                   load the OMOP vocabulary: the pinned Eunomia shard, or
                               local Athena folders (--athena / DIP_ATHENA_DIRS)
  vocab-coverage               unmapped rows and codes per OMOP table (counts only)
  deid-key-install             store DIP_DEID_KEY for the de-identified tier
  dbt <args...>                run dbt against the warehouse, e.g. `dbt build`
  extract build --version V    write a versioned, checksummed extract of omop_shareable
  extract verify --version V   rebuild (dbt build) and diff byte for byte against version V
  mcp-serve [--port P]         the MCP server (Streamable HTTP, /mcp) for the agents;
                               keys: DIP_AGENT_KEYS = {"<agent>": "<sha256 of its key>"}
  triage run                   the triage agent against an MCP server (DIP_MCP_URL, its
                               key in DIP_TRIAGE_KEY); proposals wait for a person
  triage eval [--record]       score the triage agent on agents/evals/triage/cases.yaml
                               (DIP_EVAL_DSN: a throwaway database). Replays the recorded
                               model turns by default; --record calls the model and saves them
  triage loop [--record]       every dip break scenario through detect, triage, propose,
                               approve, fix and check (agents/evals/break_loop; DIP_EVAL_DSN)
  triage act --approval ID     the triage agent runs one approval a person approved (Act
                               tier, Stage 6); the server refuses anything else
  triage act-eval [--record]   score Act mode on agents/evals/triage_act/cases.yaml
  incident run --incident ID   the incident -> triage chain (LangGraph, Stage 7): the
                               incident agent diagnoses, code decides the handoff, triage
                               proposes (DIP_MCP_URL, DIP_INCIDENT_KEY, DIP_TRIAGE_KEY)
  incident eval [--record]     score the chain on agents/evals/incident/cases.yaml
  demo [--record]              the recorded demo: one breakage through incident, handoff,
                               triage, approval, Act and resend (DIP_EVAL_DSN; scripts/demo.sh)
  approvals list|show|approve|reject|execute [ID]
                               the human side of agent proposals (audited as human:<you>)
  cohort list                  the cohort library (packs/healthcare/cohorts), latest versions
  cohort run NAME...           run library cohorts as the read-only cohort reader
                               (DIP_COHORT_DSN; Synthea tier only), suppressed counts as JSON
  cohort verify-audit          check the hash chain of the cohort audit log
  contracts sample FEED FILE   register a new vendor's sample file for the contract author
                               (Stage 5): its raw copy and its sha256, for diff_schema
  contracts dbt FEED [--write]  the dbt models generated from FEED's contract (Stage 10,
                               S10.3: dip/dbt_gen.py); prints them, --write writes them
  contracts eval [--record|--live]
                               score the contract author on agents/evals/contract_author
                               (DIP_EVAL_DSN: a throwaway database); replays recordings by
                               default. Every eval file is generated and invented
  break SCENARIO GOOD OUT [--arg k=v ...]
                               write a named breakage of a good file (dip/breakages.py:
                               rename_column, add_column, new_code, delimiter, drop_column,
                               junk_values, inject_cell, x12_segment_lost)
  metrics-serve [--port P]     warehouse metrics for Prometheus at /metrics, as the
                               read-only dip_metrics login (DIP_METRICS_DSN)

Storage is a local directory (`--store-dir`, default ./data/store) unless S3 settings are
given in the environment (DIP_S3_ENDPOINT, DIP_S3_ACCESS_KEY, DIP_S3_SECRET_KEY).
The warehouse is DIP_WAREHOUSE_DSN.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.intake import feed_of, process_landing
from dip.storage import LocalStore, ObjectStore, S3Store
from dip.warehouse import Warehouse


def _store(args: argparse.Namespace) -> ObjectStore:
    if endpoint := os.environ.get("DIP_S3_ENDPOINT"):
        import boto3

        return S3Store(
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=os.environ["DIP_S3_ACCESS_KEY"],
                aws_secret_access_key=os.environ["DIP_S3_SECRET_KEY"],
                region_name="us-east-1",
            )
        )
    return LocalStore(args.store_dir)


def _warehouse() -> Warehouse:
    import psycopg

    return Warehouse(psycopg.connect(os.environ["DIP_WAREHOUSE_DSN"]))


def _extract(args: argparse.Namespace) -> int:
    from dip import extract
    from dip.dbt_runner import run_dbt

    conn = _warehouse().conn
    if args.action == "build":
        from dip import lineage

        r = extract.build(conn, args.out, args.version, args.license)
        lineage.extract_event(
            lineage.Emitter(), os.environ["DIP_WAREHOUSE_DSN"], r, list(extract.TABLES)
        )
        state = "reproduced the existing" if r.reproduced_existing else "wrote"
        print(f"{state} extract {args.version} at {r.path} (manifest sha256 {r.manifest_sha256})")
        return 0
    published = args.out / args.version
    if not (published / extract.MANIFEST).is_file():
        print(f"no published extract at {published}", file=sys.stderr)
        return 2
    run_dbt(os.environ["DIP_WAREHOUSE_DSN"], "build")
    rebuilt = args.out / f".verify-{args.version}"
    shutil.rmtree(rebuilt, ignore_errors=True)
    for name, data in extract.build_files(conn, args.version, args.license).items():
        (rebuilt / name).parent.mkdir(parents=True, exist_ok=True)
        (rebuilt / name).write_bytes(data)
    differences = extract.diff(published, rebuilt)
    shutil.rmtree(rebuilt, ignore_errors=True)
    if differences:
        print(f"NOT REPRODUCIBLE: {len(differences)} file(s) differ: {differences}")
        return 1
    print(f"reproducible: rebuild of {args.version} is byte-identical")
    return 0


def _load_dotenv() -> None:
    """KEY=VALUE lines from ./.env into the environment (never overriding it, never printed)."""
    path = Path(".env")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _mcp_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dip.agents.mcp_server import build_server, http_app
    from dip.agents.registry import Policy
    from dip.agents.tools import build_registry

    keys = json.loads(os.environ["DIP_AGENT_KEYS"])
    registry = build_registry(_warehouse().conn, _store(args), load_contracts())
    server = build_server(registry, {agent: Policy.load(agent) for agent in keys})
    # The URL clients use (auth metadata): a server bound to all interfaces is reached
    # through a port-forward or its Service; DIP_MCP_PUBLIC_URL overrides.
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = os.environ.get("DIP_MCP_PUBLIC_URL") or f"http://{shown}:{args.port}"
    app = http_app(server, keys, url, args.allowed_host)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _approvals_serve(args: argparse.Namespace) -> int:
    import psycopg
    import uvicorn

    from dip.agents.gateway import create_app

    dsn = os.environ["DIP_WAREHOUSE_DSN"]
    token = os.environ.get("DIP_APPROVALS_TOKEN") or None
    if token is None and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("approvals-serve beyond loopback needs DIP_APPROVALS_TOKEN")
    hosts = args.allowed_host or ["127.0.0.1", "localhost"]
    app = create_app(lambda: psycopg.connect(dsn), hosts, token)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _triage(args: argparse.Namespace) -> int:
    import asyncio

    from dip.agents import evals, triage
    from dip.agents.llm import AnthropicLLM, Recorder, Recording, cost_usd

    _load_dotenv()
    if args.action in ("run", "act"):
        if args.action == "act" and args.approval is None:
            raise SystemExit("triage act needs --approval ID")

        async def go():
            async with triage.mcp_client(os.environ["DIP_MCP_URL"],
                                         os.environ["DIP_TRIAGE_KEY"]) as client:  # fmt: skip
                if args.action == "act":
                    return await triage.act(client, AnthropicLLM(), args.approval)
                return await triage.run(client, AnthropicLLM())

        r = asyncio.run(go())
        if r.stop != "end_turn":
            # The model stopped for another reason (max_tokens, the turn limit): what it
            # did is below, but it did not finish its task.
            print(f"WARNING: the agent stopped with {r.stop!r} before finishing", file=sys.stderr)
        if args.action == "act":
            print(r.summary)
            print(f"{r.turns} turns, {len(r.denied)} denied calls,"
                  f" ${cost_usd(r.usage, r.model):.4f}", file=sys.stderr)  # fmt: skip
            return 0 if not r.denied and r.stop == "end_turn" else 1
        for p in r.proposals:
            print(f"approval {p['approval_id']}: {p['action']:<24} {p['group_id']}"
                  f" ({p['root_cause']}, {p['confidence']})")  # fmt: skip
        print(r.summary)
        print(f"{r.turns} turns, {len(r.denied)} denied calls,"
              f" ${cost_usd(r.usage, r.model):.4f}", file=sys.stderr)  # fmt: skip
        return 0 if r.stop == "end_turn" else 1
    dsn = os.environ["DIP_EVAL_DSN"]
    if args.action == "loop":
        return _break_loop(args, dsn)
    if args.action == "act-eval":
        return _act_eval(args, dsn)
    source = args.cases_file or evals.CASES
    cases = [c for c in evals.load_cases(source) if not args.case or c["name"] in args.case]
    work = Path("data/tmp/triage-eval")
    results = []
    for case in cases * max(1, args.repeat if args.live else 1):
        path = _recordings(evals.RECORDINGS, args) / f"{case['name']}.json"
        if args.record:
            llm = Recorder(_live_llm(args))
        else:
            llm = _live_llm(args) if args.live else Recording(path)
        r = evals.run_case(case, dsn, llm, work)
        if args.record:
            llm.save(path)
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'} {case['name']}: {r.turns} turns,"
              f" violations {r.violations or 'none'}", file=sys.stderr)  # fmt: skip
    rep = evals.report(results)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(rep, indent=1, default=str), "utf-8")
    print(json.dumps(rep["summary"], indent=1))
    return 0 if rep["summary"]["policy_violations"] == 0 else 1


def _live_llm(args: argparse.Namespace) -> Any:
    """The live model for an eval: Claude, or with --provider gemini the free fallback
    (Stage 10, S10.1: the evals run against each provider)."""
    if getattr(args, "provider", "anthropic") == "gemini":
        from dip.agents.gemini import GeminiLLM

        return GeminiLLM()
    from dip.agents.llm import AnthropicLLM

    return AnthropicLLM()


def _recordings(base: Path, args: argparse.Namespace) -> Path:
    """Gemini's recordings sit beside Claude's, under gemini/ (S10.1)."""
    return base / "gemini" if getattr(args, "provider", "anthropic") == "gemini" else base


def _incident(args: argparse.Namespace) -> int:
    import asyncio

    from dip.agents import graph, incident_evals, triage
    from dip.agents.llm import AnthropicLLM, Recorder, Recording, cost_usd

    _load_dotenv()
    if args.action == "run":
        if args.incident is None:
            raise SystemExit("incident run needs --incident ID")
        import psycopg

        url = os.environ["DIP_MCP_URL"]
        chain = graph.Chain(
            conn=psycopg.connect(os.environ["DIP_WAREHOUSE_DSN"]),
            incident_client=lambda: triage.mcp_client(url, os.environ["DIP_INCIDENT_KEY"]),
            triage_client=lambda: triage.mcp_client(url, os.environ["DIP_TRIAGE_KEY"]),
            llm_for=lambda step: AnthropicLLM(),
        )
        state = asyncio.run(graph.run_chain(chain, args.incident))
        d = state.get("diagnosis") or {}
        print(
            f"chain {state['chain_id']}: incident {args.incident} -> {d.get('root_cause')}"
            f" ({d.get('confidence')}): {d.get('summary', '(no diagnosis reported)')}"
        )
        if state.get("handoff"):
            h = state["handoff"]
            print(f"handoff to triage: {list(h.group_ids)} ({len(h.quarantine_ids)} entries)")
            for p in (state.get("triage_run") or {}).get("proposals", []):
                print(f"approval {p['approval_id']}: {p['action']:<24} {p['group_id']}")
        else:
            print(f"no handoff: {state.get('handoff_refused') or 'not quarantined records'}")
        runs = [state.get("incident_run") or {}, state.get("triage_run") or {}]
        usage = [u for r in runs for u in r.get("usage", [])]
        model = next((r["model"] for r in runs if r.get("model")), "")
        print(f"${cost_usd(usage, model):.4f}", file=sys.stderr)
        return 0
    dsn = os.environ["DIP_EVAL_DSN"]
    results = []
    cases = [c for c in incident_evals.load_cases() if not args.case or c["name"] in args.case]
    for case in cases * max(1, args.repeat if args.live else 1):
        recorders: dict[str, Recorder] = {}

        def llm_for(step: str, name: str = case["name"], recorders: dict = recorders):
            path = _recordings(incident_evals.RECORDINGS, args) / f"{name}_{step}.json"
            if args.record:
                recorders[step] = Recorder(_live_llm(args))
                return recorders[step]
            return _live_llm(args) if args.live else Recording(path)

        r = incident_evals.run_case(case, dsn, llm_for, Path("data/tmp/incident-eval"))
        if args.record:
            for step, rec in recorders.items():
                rec.save(_recordings(incident_evals.RECORDINGS, args)
                         / f"{case['name']}_{step}.json")  # fmt: skip
        results.append(r)
        print(
            f"{'PASS' if r.passed else 'FAIL'} {case['name']}: cause {r.root_cause},"
            f" handoff {r.handed_off}, triage ok {r.triage_groups_ok},"
            f" violations {r.violations or 'none'}",
            file=sys.stderr,
        )
    rep = incident_evals.report(results)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(incident_evals.dumps(rep), "utf-8")
    print(json.dumps(rep["summary"], indent=1))
    s = rep["summary"]
    return 0 if s["policy_violations"] == 0 and s["cases_passed"] == s["cases"] else 1


def _act_eval(args: argparse.Namespace, dsn: str) -> int:
    from dip.agents import act_evals
    from dip.agents.llm import AnthropicLLM, Recorder, Recording

    doc = act_evals.load()
    results = []
    for case in doc["cases"] * max(1, args.repeat if args.live else 1):
        if args.case and case["name"] not in args.case:
            continue
        path = act_evals.RECORDINGS / f"{case['name']}.json"
        if args.record:
            llm = Recorder(AnthropicLLM())
        else:
            llm = AnthropicLLM() if args.live else Recording(path)
        r = act_evals.run_case(doc, case, dsn, llm, Path("data/tmp/triage-act-eval"))
        if args.record:
            llm.save(path)
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'} {case['name']}: ran={r.ran},"
              f" denied={r.denied_in_audit}, other ids {r.other_ids_attempted}",
              file=sys.stderr)  # fmt: skip
    rep = act_evals.report(results)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(act_evals.dumps(rep), "utf-8")
    print(json.dumps(rep["summary"], indent=1))
    return 0 if rep["summary"]["cases_passed"] == rep["summary"]["cases"] else 1


def _break_loop(args: argparse.Namespace, dsn: str) -> int:
    from dip.agents import break_loop
    from dip.agents.llm import AnthropicLLM, Recorder, Recording

    recorders: dict[str, Recorder] = {}

    def llm_for(name: str):
        if args.record:
            recorders[name] = Recorder(AnthropicLLM())
            return recorders[name]
        return AnthropicLLM() if args.live else Recording(break_loop.RECORDINGS / f"{name}.json")

    results = []
    for sc in break_loop.load_scenarios():
        if args.case and sc["name"] not in args.case:
            continue
        recorders.clear()
        r = break_loop.run_scenario(sc, dsn, llm_for, Path("data/tmp/break-loop"))
        if args.record and r.passed:
            for name, rec in recorders.items():
                rec.save(break_loop.RECORDINGS / f"{name}.json")
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'} {sc['name']}: {r.actions}", file=sys.stderr)
        for line in r.steps + [f"PROBLEM {p}" for p in r.problems]:
            print(f"    {line}", file=sys.stderr)
    rep = break_loop.report(results)
    out = args.out if args.out != Path("data/evals/triage") else Path("data/evals/break_loop")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, indent=1, default=str), "utf-8")
    print(json.dumps(rep["summary"], indent=1))
    return 0 if rep["summary"]["passed"] == rep["summary"]["scenarios"] else 1


def _contracts_eval(args: argparse.Namespace) -> int:
    from dip.agents import author_evals
    from dip.agents.llm import AnthropicLLM, Recorder, Recording

    _load_dotenv()
    dsn = os.environ["DIP_EVAL_DSN"]
    cases = [c for c in author_evals.load_cases() if not args.case or c["name"] in args.case]
    work = Path("data/tmp/contract-author-eval")
    results = []
    for case in cases * max(1, args.repeat if args.live else 1):
        path = author_evals.RECORDINGS / f"{case['name']}.json"
        if args.provider == "gemini":
            from dip.agents.gemini import GeminiLLM

            if not args.live:
                parser_error = "--provider gemini runs live only (recordings are Claude's)"
                raise SystemExit(parser_error)
            llm = GeminiLLM()
        elif args.record:
            llm = Recorder(AnthropicLLM())
        else:
            llm = AnthropicLLM() if args.live else Recording(path)
        r = author_evals.run_case(case, dsn, llm, work)
        if args.record:
            llm.save(path)
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'} {case['name']}: {r.turns} turns,"
              f" proposed {r.proposed}, loosening errors {len(r.loosening_errors)},"
              f" violations {r.violations or 'none'}", file=sys.stderr)  # fmt: skip
    rep = author_evals.report(results)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(rep, indent=1, default=str), "utf-8")
    print(json.dumps(rep["summary"], indent=1))
    return 0 if rep["summary"]["policy_violations"] == 0 else 1


def _entries(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def _approvals(args: argparse.Namespace) -> int:
    import getpass

    from dip.agents import approvals
    from dip.agents.registry import human_policy
    from dip.agents.tools import build_registry

    conn = _warehouse().conn
    if args.action == "list":
        for a in approvals.pending(conn, None if args.all else "pending"):
            p = a["payload"]
            print(f"{a['approval_id']:>5} {a['status']:<9} {a['agent_id']:<8} {a['action']:<24}"
                  f" {p.get('group_id')} ({_entries(len(p.get('quarantine_ids', [])))},"
                  f" {p.get('root_cause')}, confidence {p.get('confidence')})")  # fmt: skip
        return 0
    if args.approval_id is None:
        print("give an approval id", file=sys.stderr)
        return 2
    if args.action == "show":
        rows = [a for a in approvals.pending(conn, None) if a["approval_id"] == args.approval_id]
        print(json.dumps(rows, indent=1, default=str))
        return 0 if rows else 1
    # Decisions and execution go through the registry too, so they are audited. The person
    # is recorded as their OS user name and their database login; anyone with the warehouse
    # DSN can approve (separate approver roles are a follow-up, see docs/agents/triage.md).
    with conn.cursor() as cur:
        cur.execute("select session_user")
        (db_user,) = cur.fetchone()
    conn.commit()
    me = human_policy(f"{getpass.getuser()}@{db_user}")
    registry = build_registry(conn, _store(args), load_contracts())
    if args.action in ("approve", "reject"):
        try:
            status = approvals.decide(conn, args.approval_id, args.action == "approve",
                                      me.agent, args.note)  # fmt: skip
        except approvals.ApprovalError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        registry.audit(
            me.agent,
            None,
            f"approvals.{args.action}",
            "act",
            {"approval_id": args.approval_id},
            "allowed",
            None,
            status,
            None,
        )
        print(f"approval {args.approval_id}: {status}")
        return 0
    try:
        result = registry.call(me, None, "execute_approved_operation",
                               {"approval_id": args.approval_id})  # fmt: skip
    except approvals.ApprovalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, default=str))


def _cohort(args: argparse.Namespace) -> int:
    from dip import cohorts

    log = cohorts.AuditLog(args.audit)
    if args.action == "verify-audit":
        problems = cohorts.verify_audit(log.entries())
        print("\n".join(problems) or f"audit chain ok: {len(log.entries())} entries")
        return 1 if problems else 0
    lib = cohorts.library()
    if args.action == "list":
        for c in lib.values():
            print(f"{c.name} v{c.version}  {c.title}")
        return 0
    unknown = [n for n in args.names if n not in lib]
    if unknown or not args.names:
        print(f"name one or more of: {sorted(lib)}; unknown: {unknown}", file=sys.stderr)
        return 2
    for name in args.names:
        r = cohorts.run(lib[name], os.environ["DIP_COHORT_DSN"], log, args.requester or "")
        print(json.dumps(cohorts.as_dict(r)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dip")
    parser.add_argument("--store-dir", default="data/store")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("db-init")
    land = sub.add_parser("land")
    land.add_argument("feed")
    land.add_argument("files", nargs="+", type=Path)
    sub.add_parser("intake")
    vocab = sub.add_parser("vocab-load")
    vocab.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    # Full local Athena download(s) instead of the Eunomia shard; default DIP_ATHENA_DIRS.
    vocab.add_argument("--athena", type=Path, nargs="+", default=None)
    sub.add_parser("vocab-coverage")
    sub.add_parser("deid-key-install")
    ext = sub.add_parser("extract")
    ext.add_argument("action", choices=["build", "verify"])
    ext.add_argument("--version", required=True)
    ext.add_argument("--out", type=Path, default=Path("data/extracts"))
    # Default: CC0-1.0 for synthetic sources; ODbL-1.0 (required) for MIMIC-derived ones.
    ext.add_argument("--license", default=None)
    dbt = sub.add_parser("dbt")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER)
    mcp = sub.add_parser("mcp-serve")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8765)
    mcp.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Host header to accept (DNS-rebinding protection); default localhost",
    )
    aps = sub.add_parser("approvals-serve", help="the approvals page and audit viewer (S9.2)")
    aps.add_argument("--host", default="127.0.0.1")
    aps.add_argument("--port", type=int, default=8081)
    aps.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Host name to accept, no port (DNS-rebinding protection); default localhost",
    )
    tri = sub.add_parser("triage")
    tri.add_argument("action", choices=["run", "eval", "loop", "act", "act-eval"])
    tri.add_argument("--approval", type=int, help="act: the approval a person approved")
    tri.add_argument("--record", action="store_true", help="call the model, save its turns")
    tri.add_argument("--live", action="store_true", help="call the model, save nothing")
    tri.add_argument("--cases-file", type=Path, default=None)
    tri.add_argument("--repeat", type=int, default=1, help="live runs per case (variance)")
    tri.add_argument("--case", action="append", default=[])
    tri.add_argument("--out", type=Path, default=Path("data/evals/triage"))
    tri.add_argument("--provider", choices=["anthropic", "gemini"], default="anthropic",
                     help="the model (gemini: the free fallback; its recordings are under "
                          "recordings/gemini/)")  # fmt: skip
    demo = sub.add_parser("demo")
    demo.add_argument("--record", action="store_true", help="call the model, save its turns")
    inc = sub.add_parser("incident")
    inc.add_argument("action", choices=["run", "eval"])
    inc.add_argument("--incident", type=int, help="run: the incident to diagnose")
    inc.add_argument("--record", action="store_true", help="call the model, save its turns")
    inc.add_argument("--live", action="store_true", help="call the model, save nothing")
    inc.add_argument("--repeat", type=int, default=1, help="live runs per case (variance)")
    inc.add_argument("--case", action="append", default=[])
    inc.add_argument("--out", type=Path, default=Path("data/evals/incident"))
    inc.add_argument("--provider", choices=["anthropic", "gemini"], default="anthropic",
                     help="the model (gemini: the free fallback; its recordings are under "
                          "recordings/gemini/)")  # fmt: skip
    appr = sub.add_parser("approvals")
    appr.add_argument("action", choices=["list", "show", "approve", "reject", "execute"])
    appr.add_argument("approval_id", type=int, nargs="?")
    appr.add_argument("--note", default="")
    appr.add_argument("--all", action="store_true", help="list every status, not only pending")
    coh = sub.add_parser("cohort")
    coh.add_argument("action", choices=["list", "run", "verify-audit"])
    coh.add_argument("names", nargs="*")
    coh.add_argument("--audit", type=Path, default=Path("data/cohort_audit.jsonl"))
    coh.add_argument("--requester", default=os.environ.get("USER") or os.environ.get("USERNAME"))
    con = sub.add_parser("contracts")
    con.add_argument("action", choices=["sample", "eval", "dbt"])
    con.add_argument("--write", action="store_true", help="dbt: write the generated files")
    con.add_argument("feed", nargs="?")
    con.add_argument("file", type=Path, nargs="?")
    con.add_argument("--record", action="store_true", help="call the model, save its turns")
    con.add_argument("--live", action="store_true", help="call the model, save nothing")
    con.add_argument("--repeat", type=int, default=1, help="live runs per case (variance)")
    con.add_argument("--case", action="append", default=[])
    con.add_argument("--out", type=Path, default=Path("data/evals/contract_author"))
    con.add_argument(
        "--provider",
        choices=["anthropic", "gemini"],
        default="anthropic",
        help="the model for --live (gemini: the S5.5 fallback comparison)",
    )
    brk = sub.add_parser("break")
    brk.add_argument("scenario")
    brk.add_argument("good", type=Path)
    brk.add_argument("out", type=Path)
    brk.add_argument("--arg", action="append", default=[], help="k=v, e.g. column=plan_code")
    met = sub.add_parser("metrics-serve")
    met.add_argument("--port", type=int, default=9187)
    args = parser.parse_args(argv)

    if args.command == "db-init":
        _warehouse().init()
    elif args.command == "land":
        store = _store(args)
        for f in args.files:
            store.put(BUCKET_LANDING, f"{args.feed}/{f.name}", f.read_bytes())
        print(f"landed {len(args.files)} file(s) for {args.feed}")
    elif args.command == "intake":
        from dip import lineage

        contracts = load_contracts()
        outcomes = process_landing(_store(args), _warehouse(), contracts)
        emitter = lineage.Emitter()
        for o in outcomes:
            feed = feed_of(o.source_key)
            kind = contracts[feed].format_type if feed in contracts else "fhir"
            lineage.intake_event(emitter, os.environ["DIP_WAREHOUSE_DSN"], feed, o,
                                 bronze=lineage.bronze_table(kind))  # fmt: skip
            print(json.dumps(asdict(o)))
        statuses = [o.status for o in outcomes]
        print(
            f"{len(outcomes)} file(s): "
            + ", ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses))),
            file=sys.stderr,
        )
    elif args.command == "vocab-load":
        from dip import vocab

        conn = _warehouse().conn
        dirs = args.athena or vocab.athena_dirs()
        if dirs:
            concepts, maps_to = vocab.load_athena(conn, dirs)
        else:
            concepts, maps_to = vocab.load(conn, vocab.fetch(args.cache_dir))
        print(f"loaded {concepts} concepts and {maps_to} 'Maps to' relationships into vocab")
        print(json.dumps(vocab.provenance(conn)))
        for vocabulary_id, g in vocab.mapping_gaps(conn).items():
            print(
                f"warning: {g['without_maps_to']:,} of {g['non_standard']:,} non-standard"
                f" {vocabulary_id} concepts have no 'Maps to'. Athena exports a mapping"
                " only when both vocabularies are in the same download.",
                file=sys.stderr,
            )
    elif args.command == "vocab-coverage":
        from dip import coverage, vocab

        conn = _warehouse().conn
        print(json.dumps({"vocabulary": vocab.provenance(conn), **coverage.unmapped(conn)}))
    elif args.command == "deid-key-install":
        from dip.deid import install_key

        fp = install_key(_warehouse().conn, os.environ.get("DIP_DEID_KEY"))
        print(f"de-identification key installed (fingerprint {fp})")
    elif args.command == "extract":
        return _extract(args)
    elif args.command == "mcp-serve":
        return _mcp_serve(args)
    elif args.command == "approvals-serve":
        return _approvals_serve(args)
    elif args.command == "triage":
        return _triage(args)
    elif args.command == "approvals":
        return _approvals(args)
    elif args.command == "incident":
        return _incident(args)
    elif args.command == "demo":
        from dip import demo as demo_mod

        _load_dotenv()
        if not os.environ.get("DIP_EVAL_DSN"):
            raise SystemExit("dip demo needs DIP_EVAL_DSN (a throwaway database), or run"
                             " scripts/demo.sh, which starts one")  # fmt: skip
        from dip.contracts import REPO_ROOT

        work = REPO_ROOT / "data" / "tmp" / "demo"
        return demo_mod.run(os.environ["DIP_EVAL_DSN"], work, args.record)
    elif args.command == "contracts" and args.action == "eval":
        return _contracts_eval(args)
    elif args.command == "contracts" and args.action == "dbt":
        from dip import dbt_gen

        contracts = load_contracts()
        if args.feed not in contracts:
            parser.error(f"no contract for {args.feed!r}")
        c = contracts[args.feed]
        if made := dbt_gen.hand_written(c.feed):
            print(f"{c.feed}: its models are hand-written ({', '.join(p.stem for p in made)});"
                  " nothing is generated", file=sys.stderr)  # fmt: skip
            return 1
        files = dbt_gen.render(c)
        dbt_gen.check_paths(c.feed, files)
        if args.write:
            for rel in dbt_gen.write(files):
                print(f"wrote {rel}")
        else:
            for rel, text in sorted(files.items()):
                print(f"=== {rel}")
                print(text)
        return 0
    elif args.command == "contracts":
        from dip.agents.author_tools import register_sample

        if not (args.feed and args.file):
            parser.error("contracts sample needs FEED and FILE")
        who = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        wh = _warehouse()
        wh.init()
        print(register_sample(wh.conn, _store(args), args.feed, args.file, f"human:{who}"))
        return 0
    elif args.command == "break":
        from dip.breakages import SCENARIOS, apply

        if args.scenario not in SCENARIOS:
            parser.error(f"scenario must be one of {sorted(SCENARIOS)}")
        step: dict = {"op": args.scenario}
        for kv in args.arg:
            k, _, v = kv.partition("=")
            step[k] = int(v) if k in ("every", "row") else v  # values stay text
        if args.scenario == "late_file":
            print("late_file writes the good file unchanged: deliver it after the feed's"
                  " expected_arrival plus grace to see FEED.LATE")  # fmt: skip
        args.out.write_bytes(apply(args.good.read_bytes(), [step]))
        s = SCENARIOS[args.scenario]
        print(f"wrote {args.out}: {s.describe} ({s.kind}; intake: {s.reason_code or 'n/a'})")
        return 0
    elif args.command == "metrics-serve":
        from dip.metrics import serve

        serve(args.port)
        return 0
    elif args.command == "cohort":
        return _cohort(args)
    elif args.command == "dbt":
        from dip.dbt_runner import run_dbt

        return run_dbt(os.environ["DIP_WAREHOUSE_DSN"], *args.dbt_args, check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
