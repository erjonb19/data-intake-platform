"""Reviewer evals (Stage 10, S10.5; PLAN.md section 8.12): does the reviewer stop a faulty
candidate contract and let a good one through?

For each case in agents/evals/reviewer/cases.yaml:
1. the world of a contract-author eval case: its file in a fresh warehouse, through the
   real intake (drift) or registered as a sample (onboard);
2. an intake job for it, running, with the profile the server would build from the file
   (dip/agents/intake_tools.build_profile) and the profile worker's phi labels;
3. the case's candidate, recorded as the map worker would have submitted it and the
   validator passed it (the candidates are checked to be valid contracts that accept their
   world's file by tests/integration/test_review_evals.py, not here). A candidate file is
   only the contract: nothing in it says what the case expects;
4. the reviewer (live model or a recording, `recordings/<case>.json`) over the real MCP
   server with its own key and policy; its recorded verdict is scored.

Scores: the verdict is in the case's `expect`; no tool outside the policy attempted.
Faulty cases must get `revise` or `reject`; good cases should get `pass` (a good case sent
back is a false revise; a case with no verdict is counted on its own). Only the verdict is
scored, not which issue the reasons name.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dip.agents import author_evals
from dip.agents.evals import LocalServer
from dip.agents.llm import LLM, PROMPT_DIR, cost_usd
from dip.contracts import REPO_ROOT

EVALS = REPO_ROOT / "agents" / "evals" / "reviewer"
RECORDINGS = EVALS / "recordings"
PROMPT = (PROMPT_DIR / "reviewer.md").read_text(encoding="utf-8")


def load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load((EVALS / "cases.yaml").read_text(encoding="utf-8"))["cases"]


@dataclass
class ReviewScore:
    case: str
    faulty: bool
    expect: list[str]
    verdict: str | None
    reasons: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    turns: int = 0
    stop: str = ""
    usage: list[dict[str, int]] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.verdict in self.expect and not self.violations


def _profile(conn: Any, store: Any, feed: str, sha: str, phi: list[str]) -> dict[str, Any]:
    from dip import BUCKET_RAW
    from dip.agents.intake_tools import build_profile
    from dip.contract_author import observe

    contracts = author_evals.eval_contracts()
    with conn.cursor() as cur:
        cur.execute(
            "select raw_key, source_key from ops.intake_file where feed = %s and file_sha256 = %s"
            " union all select raw_key, name from ops.sample_file where feed = %s"
            " and file_sha256 = %s",
            (feed, sha, feed, sha),
        )
        raw_key, name = cur.fetchone()
    conn.commit()
    contract = contracts.get(feed)
    if contract:
        fmt = contract.spec["format"]
    elif name.lower().endswith(".xlsx"):
        fmt = {"type": "excel", "header_row": 1}
    else:
        fmt = {"type": "csv", "delimiter": ",", "encoding": "utf-8", "header": True}
    profile = build_profile(observe(contract, store.get(BUCKET_RAW, raw_key), fmt),
                            Path(name).name)  # fmt: skip
    declared = {c["name"] for c in (contract.spec["columns"] if contract else []) if c.get("phi")}
    for c in profile["columns"]:
        c["phi"] = c["column"] in declared or c["column"] in phi
        c["role"] = "other"
    return profile


def run_case(case: dict[str, Any], dsn: str, llm: LLM, work: Path | None = None) -> ReviewScore:
    import psycopg

    from dip.agents import triage
    from dip.intake_jobs import claim_one, queue

    world = {c["name"]: c for c in author_evals.load_cases()}[case["world"]]
    work = work or Path(tempfile.mkdtemp())
    chain = f"review-{case['name']}"
    text = (EVALS / "candidates" / f"{case['candidate']}.yaml").read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        store, sha = author_evals.build_state(conn, work / case["name"], world)
        with conn.cursor() as cur:
            queue(cur, world["feed"], world["mode"], sha, "cli", "review-eval")
        conn.commit()
        job = claim_one(conn, "review-eval")
        assert job is not None
        profile = _profile(conn, store, world["feed"], sha, case.get("phi", []))
        cid = hashlib.sha256(f"{world['feed']}|{sha}|{text}".encode()).hexdigest()[:16]
        with conn.cursor() as cur:
            cur.execute("update ops.intake_job set chain_id = %s, profile = %s where job_id = %s",
                        (chain, json.dumps(profile), job["job_id"]))  # fmt: skip
            cur.execute(
                "insert into ops.contract_candidate (chain_id, candidate_id, feed, file_sha256,"
                " contract_yaml, created_by, submitted, rationale, validator_valid, dbt_ok)"
                " values (%s, %s, %s, %s, %s, 'contract_author', true, %s, true, true)",
                (chain, cid, world["feed"], sha, text, case["rationale"]),
            )
        conn.commit()
        with LocalServer(conn, store, "reviewer", author_evals.eval_contracts()) as srv:
            started = time.monotonic()
            task = (
                f"Intake job {job['job_id']}, feed `{world['feed']}` ({world['mode']}): "
                f"review candidate {cid} (get_review_packet), then report_review."
            )

            async def go() -> triage.TriageRun:
                async with triage.mcp_client(srv.url, srv.key) as client:
                    return await triage.run_agent(client, llm, PROMPT, task, 20,
                                                  f"{chain}/review")  # fmt: skip

            run = asyncio.run(go())
        with conn.cursor() as cur:
            cur.execute("select review from ops.contract_candidate where chain_id = %s"
                        " and candidate_id = %s", (chain, cid))  # fmt: skip
            (review,) = cur.fetchone()
        conn.commit()
    review = review or {}
    return ReviewScore(
        case=case["name"], faulty="faulty" in case.get("tags", []), expect=case["expect"],
        verdict=review.get("verdict"), reasons=review.get("reasons", []),
        violations=[f"{c.name}: {c.result[:120]}" for c in run.denied], turns=run.turns,
        stop=run.stop, usage=run.usage, model=run.model,
        seconds=round(time.monotonic() - started, 1),
    )  # fmt: skip


def report(results: list[ReviewScore]) -> dict[str, Any]:
    faulty = [r for r in results if r.faulty]
    good = [r for r in results if not r.faulty]
    usage = [u for r in results for u in r.usage]
    model = next((r.model for r in results if r.model), "")
    stopped = sum(r.verdict in ("revise", "reject") for r in faulty)
    return {
        "summary": {
            "model": model, "cases": len(results), "cases_passed": sum(r.passed for r in results),
            "faulty_stopped": f"{stopped}/{len(faulty)}",
            "good_sent_back": f"{sum(r.verdict in ('revise', 'reject') for r in good)}/{len(good)}",
            "no_verdict": sum(r.verdict is None for r in results),
            "policy_violations": sum(len(r.violations) for r in results),
            "cost_usd": cost_usd(usage, model),
        },
        "cases": [asdict(r) | {"passed": r.passed} for r in results],
    }  # fmt: skip
